"""Outbox/transport crash boundaries, strict envelopes, and safe topology."""

import json
from types import SimpleNamespace

import pytest
from test_document_jobs import store_at


def test_strict_message_validation():
    from medical_rag.messaging import decode_message

    valid = {
        "schema_version": 1,
        "message_id": "abc",
        "task_type": "document.ingest",
        "task_id": "123",
        "dispatch_seq": 0,
        "trace_id": "123",
    }
    assert decode_message(json.dumps(valid).encode()) == valid
    for bad in (
        {**valid, "question": "secret"},
        {**valid, "dispatch_seq": True},
        {**valid, "schema_version": 2},
        {**valid, "task_type": "unknown"},
    ):
        with pytest.raises(ValueError):
            decode_message(json.dumps(bad).encode())
    with pytest.raises(ValueError):
        decode_message(b'{"task_id":"x","task_id":"y"}')


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "message_id",
        "task_type",
        "task_id",
        "dispatch_seq",
        "trace_id",
    ],
)
@pytest.mark.parametrize("value", [None, [], {}, True, 1.5])
def test_invalid_field_shapes_always_raise_value_error(field, value):
    from medical_rag.messaging import decode_message

    payload = {
        "schema_version": 1,
        "message_id": "abc",
        "task_type": "document.ingest",
        "task_id": "abc",
        "dispatch_seq": 0,
        "trace_id": "abc",
    }
    payload[field] = value
    with pytest.raises(ValueError):
        decode_message(json.dumps(payload).encode())


@pytest.mark.asyncio
async def test_relay_confirms_before_mark_and_republishes_after_crash(tmp_path):
    from medical_rag.messaging import OutboxRelay

    store = store_at(tmp_path)
    store.submit("a.txt", b"x", "same")
    store.submit("b.txt", b"y", "same")
    published = []

    class Publisher:
        async def publish(self, payload, dead=False):
            published.append(payload)
            if len(published) == 1:
                raise ConnectionError("broker disconnected after publication")

    relay = OutboxRelay(store, Publisher())
    assert await relay.process_once() is False
    with store.connect(True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM task_outbox WHERE sent_at IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        db.execute("UPDATE task_outbox SET lease_until=0")
    assert await relay.process_once()
    assert published[0] == published[1]
    assert not await relay.process_once()  # newer source job is withheld


@pytest.mark.asyncio
async def test_consumer_ack_commit_and_reject_malformed(tmp_path):
    from medical_rag.messaging import consume_message

    events = []

    class Message:
        body = b"not JSON"

        async def ack(self):
            events.append("ack")

        async def reject(self, requeue=False):
            events.append(("reject", requeue))

    message = Message()
    await consume_message(
        message, lambda task, seq: events.append("committed"), "document.ingest"
    )
    assert events == [("reject", False)]
    events.clear()
    message.body = json.dumps(
        {
            "schema_version": 1,
            "message_id": "abc",
            "task_type": "document.ingest",
            "task_id": "123",
            "dispatch_seq": 0,
            "trace_id": "123",
        }
    ).encode()
    await consume_message(
        message, lambda task, seq: events.append("committed"), "document.ingest"
    )
    assert events == ["committed", "ack"]
    events.clear()

    def storage_failure(*args):
        raise RuntimeError("not committed")

    with pytest.raises(RuntimeError):
        await consume_message(message, storage_failure, "document.ingest")
    assert events == []  # close connection; delivery remains unfinished


@pytest.mark.asyncio
async def test_topology_is_durable_quorum_with_safe_dead_lettering():
    from medical_rag.messaging import declare_topology

    exchanges, queues = {}, {}

    class Queue:
        async def bind(self, exchange, routing_key):
            pass

    class Channel:
        async def set_qos(self, prefetch_count):
            assert prefetch_count == 1

        async def declare_exchange(self, name, kind, durable):
            exchanges[name] = (kind, durable)
            return name

        async def declare_queue(self, name, durable, arguments):
            queues[name] = arguments
            return Queue()

    await declare_topology(Channel())
    assert set(exchanges) == {"medical_rag.tasks", "medical_rag.dead"}
    assert len(queues) == 4
    for name, args in queues.items():
        assert args["x-queue-type"] == "quorum"
        assert args["x-quorum-initial-group-size"] == 1
        if name.endswith(("ingest", "extract")):
            assert args["x-dead-letter-strategy"] == "at-least-once"
            assert args["x-overflow"] == "reject-publish"


def test_expired_document_and_memory_claims_recover_with_new_generation(tmp_path):
    from test_memory import enqueue

    from medical_rag.memory import MemoryStore
    from medical_rag.messaging import OutboxRelay

    store = store_at(tmp_path)
    task = store.submit("a.txt", b"x")
    first = store.claim(task["job_id"], 0)
    memory = MemoryStore(store.path, tmp_path / "episodes")
    enqueue(memory, "memory-one")
    memory.claim("memory-one", 0)
    with store.connect(True) as db:
        db.execute("UPDATE document_jobs SET lease_until=0")
        db.execute("UPDATE memory_jobs SET lease_until=0")
    relay = OutboxRelay(store, None)
    relay.recover_expired()
    assert not store.claim(task["job_id"], 0)
    assert store.claim(task["job_id"], 1)["attempts"] == 2
    assert not memory.claim("memory-one", 0)
    assert memory.claim("memory-one", 1)["attempts"] == 2
    store.finish(first, result={"stale": True})
    assert store.get(task["job_id"])["status"] == "running"
    with store.connect(True) as db:
        db.execute("UPDATE document_jobs SET lease_until=0,attempts=3")
        db.execute("UPDATE memory_jobs SET lease_until=0,attempts=3")
    relay.recover_expired()
    assert store.get(task["job_id"])["status"] == "failed"
    assert memory.job_status("memory-one")["status"] == "failed"
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM task_outbox WHERE destination='dead'"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.asyncio
async def test_transport_confirm_failure_and_close(monkeypatch):
    import aio_pika

    from medical_rag.messaging import AMQPTransport

    events = []

    class Exchange:
        async def publish(self, message, **kwargs):
            assert kwargs["mandatory"] is True
            assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
            return False

    class Channel:
        async def declare_exchange(self, *args, **kwargs):
            return Exchange()

        async def declare_queue(self, *args, **kwargs):
            return SimpleNamespace(bind=self.bind)

        async def bind(self, *args, **kwargs):
            pass

        async def set_qos(self, **kwargs):
            pass

    class Connection:
        async def channel(self, **kwargs):
            assert kwargs == {"publisher_confirms": True, "on_return_raises": True}
            return Channel()

        async def close(self):
            events.append("close")

    async def connect(*args, **kwargs):
        return Connection()

    monkeypatch.setattr(aio_pika, "connect", connect)
    transport = AMQPTransport("amqp://not-used")
    payload = {
        "schema_version": 1,
        "message_id": "a",
        "task_type": "document.ingest",
        "task_id": "b",
        "dispatch_seq": 0,
        "trace_id": "b",
    }
    with pytest.raises(ConnectionError):
        await transport.publish(payload)
    await transport.close()
    assert events == ["close"] and transport.connection is None


@pytest.mark.parametrize("failure", ["stat", "unlink", "iterdir"])
@pytest.mark.asyncio
async def test_cleanup_filesystem_failure_never_blocks_valid_dispatch(
    tmp_path, monkeypatch, failure
):
    import os
    from pathlib import Path

    from medical_rag.messaging import OutboxRelay

    store = store_at(tmp_path)
    task = store.submit("active.txt", b"keep")
    active = store.upload_root / (task["job_id"] + ".upload")
    orphan = store.upload_root / "broken.upload"
    orphan.write_bytes(b"old")
    os.utime(active, (0, 0))
    os.utime(orphan, (0, 0))
    original = getattr(Path, failure)

    def broken(path, *args, **kwargs):
        if path == (store.upload_root if failure == "iterdir" else orphan):
            raise OSError("sensitive local path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, failure, broken)
    published = []

    class Publisher:
        async def publish(self, payload, dead=False):
            published.append(payload)

    store.cleanup_orphans()
    assert await OutboxRelay(store, Publisher()).process_once()
    assert published[0]["task_id"] == task["job_id"]
    assert active.read_bytes() == b"keep"
