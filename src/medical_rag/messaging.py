"""Confirmed AMQP transport; business state lives exclusively in SQLite."""

import asyncio
import json
import re
import time
from uuid import uuid4

from .jobs import DocumentJobStore, enqueue_task

TASKS_EXCHANGE = "medical_rag.tasks"
DEAD_EXCHANGE = "medical_rag.dead"
QUEUES = {
    "document.ingest": "medical_rag.document.ingest",
    "memory.extract": "medical_rag.memory.extract",
}
DEAD_QUEUES = {
    "document.ingest": "medical_rag.document.dead",
    "memory.extract": "medical_rag.memory.dead",
}
FIELDS = {
    "schema_version",
    "message_id",
    "task_type",
    "task_id",
    "dispatch_seq",
    "trace_id",
}


def decode_message(body: bytes) -> dict:
    """Reject unknown fields, duplicate keys and all non-envelope business data."""
    if len(body) > 4096:
        raise ValueError("Oversized task envelope")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate message key")
            result[key] = value
        return result

    try:
        payload = json.loads(body, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid task JSON") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != FIELDS
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or type(payload["dispatch_seq"]) is not int
        or not 0 <= payload["dispatch_seq"] <= 1000000
        or not isinstance(payload["task_type"], str)
        or payload["task_type"] not in QUEUES
    ):
        raise ValueError("Invalid task envelope")
    for key in ("message_id", "task_id", "trace_id"):
        if not isinstance(payload[key], str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", payload[key]
        ):
            raise ValueError("Invalid task identifier")
    return payload


async def declare_topology(channel) -> dict:
    """Quorum single-node queues with durable at-least-once dead-lettering."""
    tasks = await channel.declare_exchange(TASKS_EXCHANGE, "direct", durable=True)
    dead = await channel.declare_exchange(DEAD_EXCHANGE, "direct", durable=True)
    queues = {}
    for task_type, name in QUEUES.items():
        base = {"x-queue-type": "quorum", "x-quorum-initial-group-size": 1}
        dlq = await channel.declare_queue(
            DEAD_QUEUES[task_type], durable=True, arguments=base.copy()
        )
        await dlq.bind(dead, routing_key=task_type)
        arguments = {
            **base,
            "x-dead-letter-exchange": DEAD_EXCHANGE,
            "x-dead-letter-routing-key": task_type,
            "x-dead-letter-strategy": "at-least-once",
            "x-overflow": "reject-publish",
        }
        queue = await channel.declare_queue(name, durable=True, arguments=arguments)
        await queue.bind(tasks, routing_key=task_type)
        queues[task_type] = queue
    await channel.set_qos(prefetch_count=1)
    return {"tasks": tasks, "dead": dead, "queues": queues}


class AMQPTransport:
    """One role's reconnectable publisher/consumer, with secrets absent from logs."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.connection = None
        self.channel = None
        self.topology = None

    async def connect(self):
        import aio_pika

        self.connection = await aio_pika.connect(self.url, timeout=10)
        self.channel = await self.connection.channel(
            publisher_confirms=True, on_return_raises=True
        )
        self.topology = await declare_topology(self.channel)
        return self

    async def close(self):
        if self.connection:
            await self.connection.close()
        self.connection = self.channel = self.topology = None

    async def publish(self, payload: dict, dead: bool = False) -> None:
        import aio_pika

        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        decode_message(encoded)
        if self.topology is None:
            await self.connect()
        confirmation = await self.topology["dead" if dead else "tasks"].publish(
            aio_pika.Message(
                encoded,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=payload["message_id"],
                correlation_id=payload["trace_id"],
            ),
            routing_key=payload["task_type"],
            mandatory=True,
            timeout=10,
        )
        if confirmation is False or confirmation is None:
            raise ConnectionError("Task publication was not confirmed")


class OutboxRelay:
    """Lease a dispatch, confirm it, then mark sent (never the other order)."""

    def __init__(self, store: DocumentJobStore, publisher) -> None:
        self.store = store
        self.publisher = publisher
        self.publish_failed = False

    def recover_expired(self) -> None:
        now = time.time()
        with self.store.connect(True) as db:
            rows = db.execute(
                "SELECT * FROM document_jobs WHERE status='running' AND lease_until<?",
                (now,),
            ).fetchall()
            for row in rows:
                terminal = row["attempts"] >= 3
                sequence = row["dispatch_seq"] + 1
                db.execute(
                    "UPDATE document_jobs SET status=?,stage=?,token=NULL,dispatch_seq=?,available_at=?,updated_at=?,error=? WHERE job_id=?",
                    (
                        "failed" if terminal else "retrying",
                        "failed" if terminal else "retrying",
                        sequence,
                        now,
                        now,
                        json.dumps(
                            {"code": "WORKER_INTERRUPTED", "message": "任务执行中断"}
                        )
                        if terminal
                        else None,
                        row["job_id"],
                    ),
                )
                enqueue_task(
                    db, "document.ingest", row["job_id"], sequence, now, dead=terminal
                )
            # Memory's schema may not exist in a jobs-only process yet.
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='memory_jobs'"
            ).fetchone():
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(memory_jobs)")
                }
                if "dispatch_seq" in columns:
                    memory = db.execute(
                        "SELECT * FROM memory_jobs WHERE status='processing' AND lease_until<?",
                        (now,),
                    ).fetchall()
                    for row in memory:
                        terminal = row["attempts"] >= 3
                        seq = row["dispatch_seq"] + 1
                        db.execute(
                            "UPDATE memory_jobs SET status=?,token=NULL,dispatch_seq=?,available=?,question=CASE WHEN ? THEN '' ELSE question END,"
                            "output=CASE WHEN ? THEN '{}' ELSE output END WHERE run_id=?",
                            (
                                "failed" if terminal else "pending",
                                seq,
                                now,
                                terminal,
                                terminal,
                                row["run_id"],
                            ),
                        )
                        enqueue_task(
                            db, "memory.extract", row["run_id"], seq, now, dead=terminal
                        )

    def claim(self) -> dict | None:
        now = time.time()
        with self.store.connect(True) as db:
            rows = db.execute(
                "SELECT * FROM task_outbox WHERE sent_at IS NULL AND available_at<=? AND lease_until<=? "
                "ORDER BY available_at,message_id",
                (now, now),
            ).fetchall()
            for row in rows:
                if (
                    row["destination"] == "tasks"
                    and row["task_type"] == "document.ingest"
                ):
                    job = db.execute(
                        "SELECT * FROM document_jobs WHERE job_id=?", (row["task_id"],)
                    ).fetchone()
                    if (
                        not job
                        or job["status"] in ("succeeded", "failed")
                        or job["dispatch_seq"] != row["dispatch_seq"]
                    ):
                        db.execute(
                            "UPDATE task_outbox SET sent_at=? WHERE message_id=?",
                            (now, row["message_id"]),
                        )
                        continue
                    older = db.execute(
                        "SELECT 1 FROM document_jobs WHERE source_id=? AND job_order<? AND status NOT IN ('succeeded','failed')",
                        (job["source_id"], job["job_order"]),
                    ).fetchone()
                    if older:
                        continue
                token = uuid4().hex
                db.execute(
                    "UPDATE task_outbox SET token=?,lease_until=? WHERE message_id=?",
                    (token, now + 30, row["message_id"]),
                )
                return {**dict(row), "token": token}
        return None

    async def process_once(self) -> bool:
        self.publish_failed = False
        row = self.claim()
        if row is None:
            return False
        try:
            await self.publisher.publish(
                json.loads(row["payload"]), dead=row["destination"] == "dead"
            )
        except Exception:  # noqa: BLE001 - all publication failures retain the durable dispatch
            # Lease remains finite. Lost confirm deliberately permits duplicate delivery.
            self.publish_failed = True
            return False
        with self.store.connect(True) as db:
            db.execute(
                "UPDATE task_outbox SET sent_at=?,token=NULL,lease_until=0 WHERE message_id=? AND token=?",
                (time.time(), row["message_id"], row["token"]),
            )
        return True


async def consume_message(message, processor, expected_type: str) -> None:
    """Only ACK after authoritative processor commit; malformed payloads go to DLQ."""
    try:
        task = decode_message(message.body)
        if task["task_type"] != expected_type:
            raise ValueError("Wrong task route")
    except ValueError:
        await message.reject(requeue=False)
        return
    # Cancellation must not release the ingest singleton while a sync writer lives.
    execution = asyncio.create_task(
        asyncio.to_thread(processor, task["task_id"], task["dispatch_seq"])
    )
    try:
        await asyncio.shield(execution)
    except asyncio.CancelledError:
        await execution
        raise
    await message.ack()
