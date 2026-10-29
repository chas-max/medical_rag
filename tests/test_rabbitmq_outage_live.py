"""Explicit opt-in outage drill against a disposable, labeled test container."""

import asyncio
import json
import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from test_api import FakeService
from test_rabbitmq_e2e import IngestAdapter, dedicated_url

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.jobs import DocumentProcessor
from medical_rag.messaging import AMQPTransport, consume_message
from medical_rag.worker import run_role

pytestmark = pytest.mark.usefixtures("rabbitmq_namespace")


@pytest.mark.asyncio
async def test_upload_during_broker_outage_is_automatically_dispatched_after_recovery(tmp_path, monkeypatch):
    url = dedicated_url()
    container = os.environ.get("TEST_RABBITMQ_CONTAINER", "")
    if not container.startswith("medical-rag-rabbitmq-test-"):
        pytest.skip("explicit disposable TEST_RABBITMQ_CONTAINER required")
    inspection = await asyncio.to_thread(
        subprocess.run, ["docker", "inspect", container], check=True,
        capture_output=True, text=True,
    )
    label = json.loads(inspection.stdout)[0]["Config"]["Labels"].get("codex.task")
    assert label == "rabbitmq-integration", "refusing to pause an unlabeled container"
    monkeypatch.setenv("CHAT_HISTORY_FILE", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("DOCUMENT_UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("MEMORY_EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("RABBITMQ_URL", url)
    app = create_app(service=FakeService())
    store = app.state.document_jobs
    transport = AMQPTransport(url)
    relay_task = None
    paused = False
    failed_connect = asyncio.Event()

    class ObservedTransport(AMQPTransport):
        async def connect(self):
            try:
                return await super().connect()
            except (OSError, TimeoutError, ConnectionError):
                failed_connect.set()
                raise

    monkeypatch.setattr("medical_rag.worker.AMQPTransport", ObservedTransport)
    try:
        await asyncio.to_thread(subprocess.run, ["docker", "pause", container], check=True,
                                capture_output=True)
        paused = True
        relay_task = asyncio.create_task(run_role("relay", Settings()))
        with TestClient(app) as client:
            response = client.post("/documents", files={"file": ("guide.txt", b"public document")})
            assert response.status_code == 202
            job_id = response.json()["data"]["job_id"]
            assert store.get(job_id)["status"] == "queued"
        await asyncio.wait_for(failed_connect.wait(), timeout=25)
        assert store.get(job_id)["attempts"] == 0
        await asyncio.to_thread(subprocess.run, ["docker", "unpause", container], check=True,
                                capture_output=True)
        paused = False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with store.connect() as db:
                sent = db.execute("SELECT sent_at FROM task_outbox WHERE task_id=?", (job_id,)).fetchone()[0]
            if sent is not None:
                break
            await asyncio.sleep(0.1)
        assert sent is not None, "relay did not automatically publish after broker recovery"
        await transport.connect()
        message = await transport.topology["queues"]["document.ingest"].get(timeout=10)
        adapter = IngestAdapter()
        await consume_message(message, DocumentProcessor(store, service=adapter).process, "document.ingest")
        assert store.get(job_id)["status"] == "succeeded"
        assert adapter.calls == 1
    finally:
        if paused:
            await asyncio.to_thread(subprocess.run, ["docker", "unpause", container], check=True,
                                    capture_output=True)
        if relay_task:
            relay_task.cancel()
            await asyncio.gather(relay_task, return_exceptions=True)
        await transport.close()
