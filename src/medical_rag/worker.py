"""Run a native role with ``python -m medical_rag.worker relay|ingest|memory``."""

import argparse
import asyncio
import logging
import sqlite3

from .config import Settings
from .jobs import DocumentJobStore, DocumentProcessor, IngestLock
from .memory import MemoryStore
from .memory_worker import MemoryWorker
from .messaging import AMQPTransport, OutboxRelay, consume_message
from .types import ApiError

logger = logging.getLogger(__name__)


def _require_demo_access(settings: Settings) -> None:
    if settings.access_mode != "unsafe-local-demo":
        raise RuntimeError("Worker disabled: security isolation is not ready")


def fresh_memory_transport():
    """Reload env defaults before applying profiles, including after deletion."""
    from .generation import OpenAITransport
    from .settings_store import ModelSettings

    settings = Settings()
    ModelSettings(settings).apply_saved()
    return OpenAITransport(settings)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Durable RAG task workers")
    result.add_argument("role", choices=("relay", "ingest", "memory"))
    return result


async def run_role(role: str, settings: Settings | None = None) -> None:
    """Reconnect on transport failure; cancellation drains any active sync writer."""
    settings = settings or Settings()
    _require_demo_access(settings)
    store = DocumentJobStore(settings)
    memory = MemoryStore(settings.chat_history_file, settings.memory_episodes_dir)
    with memory._connect():
        pass  # Initialize/migrate valid unfinished memory tasks even if API is idle.
    transport = AMQPTransport(settings.rabbitmq_url)
    state = {"broker": "disconnected"}

    def record_heartbeat(broker: str) -> None:
        try:
            store.heartbeat(role, broker)
        except (ApiError, OSError, sqlite3.Error):
            # Optional health telemetry must neither stop work nor kill its loop.
            logger.warning("%s worker heartbeat unavailable; retrying later", role)

    async def close_transport() -> None:
        try:
            await transport.close()
        except Exception:  # noqa: BLE001 - adapter close failure must not mask cancellation or prevent reconnect
            logger.warning("%s worker transport cleanup unavailable", role)

    async def heartbeat():
        while True:
            record_heartbeat(state["broker"])
            await asyncio.sleep(10)

    beat = asyncio.create_task(heartbeat())
    processor = (
        DocumentProcessor(store)
        if role == "ingest"
        else MemoryWorker(memory, transport_factory=fresh_memory_transport)
    )
    try:
        while True:
            try:
                await transport.connect()
                state["broker"] = "connected"
                record_heartbeat("connected")
                if role == "relay":
                    relay = OutboxRelay(store, transport)
                    while True:
                        relay.recover_expired()
                        try:
                            store.cleanup_orphans()
                        except (ApiError, OSError, sqlite3.Error):
                            logger.warning(
                                "Upload maintenance unavailable; retrying later"
                            )
                        worked = await relay.process_once()
                        if relay.publish_failed:
                            raise ConnectionError("Publisher connection unavailable")
                        if not worked:
                            await asyncio.sleep(1)
                else:
                    task_type = (
                        "document.ingest" if role == "ingest" else "memory.extract"
                    )
                    queue = transport.topology["queues"][task_type]
                    async with queue.iterator() as messages:
                        async for message in messages:
                            await consume_message(message, processor.process, task_type)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect with sanitized operational logging
                state["broker"] = "disconnected"
                logger.warning("%s worker connection unavailable; retrying", role)
                await close_transport()
                record_heartbeat("disconnected")
                await asyncio.sleep(1)
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        await close_transport()
        record_heartbeat("stopped")


def main() -> None:
    role = parser().parse_args().role
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings()
    _require_demo_access(settings)
    try:
        if role == "ingest":
            # asyncio.run waits for its threadpool on cancellation; OS lock stays held.
            path = DocumentJobStore(settings).path.with_suffix(".ingest.lock")
            with IngestLock(path):
                asyncio.run(run_role(role, settings))
        else:
            asyncio.run(run_role(role, settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
