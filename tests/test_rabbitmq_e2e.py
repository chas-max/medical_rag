"""Real broker + real SQLite/files, with only the expensive ingest adapter replaced."""

import os
from urllib.parse import unquote, urlsplit

import pytest

from medical_rag.config import Settings
from medical_rag.jobs import DocumentJobStore, DocumentProcessor
from medical_rag.messaging import AMQPTransport, OutboxRelay, consume_message

pytestmark = pytest.mark.usefixtures("rabbitmq_namespace")


class IngestAdapter:
    def __init__(self):
        self.calls = 0

    def ingest(self, filename, content, source_id):
        self.calls += 1
        assert content == b"public document"
        return {"document_id": "document", "chunk_count": 1, "strategy": "recursive",
                "reason": "", "duplicate": False}


def dedicated_url():
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("explicit TEST_RABBITMQ_URL required")
    if not unquote(urlsplit(url).path.lstrip("/")).startswith("test-medical-rag-"):
        pytest.fail("refusing to test against a non-test vhost")
    return url


@pytest.mark.asyncio
async def test_lost_publish_confirmation_replays_but_ingests_only_once(tmp_path):
    url = dedicated_url()
    jobs = DocumentJobStore(Settings(_env_file=None,
        chat_history_file=str(tmp_path / "jobs.sqlite3"), document_uploads_dir=str(tmp_path / "uploads")))
    job = jobs.submit("guide.txt", b"public document")
    transport = AMQPTransport(url)
    await transport.connect()

    class LostConfirm:
        first = True

        async def publish(self, payload, dead=False):
            await transport.publish(payload, dead=dead)
            if self.first:
                self.first = False
                raise ConnectionError("simulated lost publisher confirmation")

    try:
        relay = OutboxRelay(jobs, LostConfirm())
        assert await relay.process_once() is False
        with jobs.connect(True) as db:
            row = db.execute("SELECT sent_at FROM task_outbox").fetchone()
            assert row[0] is None
            db.execute("UPDATE task_outbox SET lease_until=0")
        assert await relay.process_once()
        adapter = IngestAdapter()
        processor = DocumentProcessor(jobs, service=adapter)
        queue = transport.topology["queues"]["document.ingest"]
        for _ in range(2):
            message = await queue.get(timeout=10)
            await consume_message(message, processor.process, "document.ingest")
        assert jobs.get(job["job_id"])["status"] == "succeeded"
        assert adapter.calls == 1
        assert not list(jobs.upload_root.glob("*.upload"))
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_lost_ack_redelivery_uses_committed_result_after_reconnect(tmp_path):
    url = dedicated_url()
    jobs = DocumentJobStore(Settings(_env_file=None,
        chat_history_file=str(tmp_path / "jobs.sqlite3"), document_uploads_dir=str(tmp_path / "uploads")))
    job = jobs.submit("guide.txt", b"public document")
    transport = AMQPTransport(url)
    await transport.connect()
    adapter = IngestAdapter()
    processor = DocumentProcessor(jobs, service=adapter)
    try:
        assert await OutboxRelay(jobs, transport).process_once()
        message = await transport.topology["queues"]["document.ingest"].get(timeout=10)

        class LostAck:
            body = message.body

            async def ack(self):
                raise ConnectionError("simulated ACK connection loss")

        with pytest.raises(ConnectionError):
            await consume_message(LostAck(), processor.process, "document.ingest")
        assert jobs.get(job["job_id"])["status"] == "succeeded"
        await transport.close()  # Actual broker requeues the original unacknowledged delivery.
        await transport.connect()
        repeated = await transport.topology["queues"]["document.ingest"].get(timeout=10)
        assert repeated.redelivered
        await consume_message(repeated, processor.process, "document.ingest")
        assert adapter.calls == 1
    finally:
        await transport.close()
