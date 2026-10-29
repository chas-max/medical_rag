"""Worker CLI roles and fresh per-extraction model selection."""

import asyncio
from types import SimpleNamespace

import pytest


def test_worker_parser_has_three_roles():
    from medical_rag.worker import parser

    for role in ("relay", "ingest", "memory"):
        assert parser().parse_args([role]).role == role


def test_fresh_transport_falls_back_after_profile_deletion(tmp_path, monkeypatch):
    from medical_rag import generation
    from medical_rag.settings_store import SettingsStore
    from medical_rag.worker import fresh_memory_transport

    monkeypatch.setenv("SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("QWEN_MODEL", "env-model")
    monkeypatch.setattr(generation, "OpenAITransport", lambda settings: settings)
    settings_store = SettingsStore(tmp_path / "settings.json")
    settings_store.write_section(
        "model",
        {
            "active": "p",
            "profiles": [
                {
                    "id": "p",
                    "model_name": "saved-model",
                    "base_url": "https://example.test/v1",
                    "api_key": "secret",
                }
            ],
        },
    )
    assert fresh_memory_transport().qwen_model == "saved-model"
    settings_store.write_section("model", {"profiles": [], "active": None})
    assert fresh_memory_transport().qwen_model == "env-model"


@pytest.mark.parametrize("role", ["relay", "ingest", "memory"])
@pytest.mark.asyncio
async def test_role_heartbeats_and_shutdown_drain(tmp_path, monkeypatch, role):
    from medical_rag import worker
    from medical_rag.config import Settings

    events = []

    class Messages:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            events.append("iterator-closed")

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise asyncio.CancelledError

    class Transport:
        def __init__(self, url):
            self.topology = {
                "queues": {
                    task: SimpleNamespace(iterator=lambda: Messages())
                    for task in ("document.ingest", "memory.extract")
                }
            }

        async def connect(self):
            events.append("connected")

        async def close(self):
            events.append("closed")

    class Relay:
        def __init__(self, *args):
            self.publish_failed = False

        def recover_expired(self):
            events.append("recover")

        async def process_once(self):
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "AMQPTransport", Transport)
    monkeypatch.setattr(worker, "OutboxRelay", Relay)
    settings = Settings(
        chat_history_file=str(tmp_path / "history.db"),
        document_uploads_dir=str(tmp_path / "uploads"),
        memory_episodes_dir=str(tmp_path / "episodes"),
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run_role(role, settings)
    assert events[0] == "connected" and events[-1] == "closed"
    store = worker.DocumentJobStore(settings)
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT broker FROM worker_heartbeats WHERE role=?", (role,)
            ).fetchone()[0]
            == "stopped"
        )


@pytest.mark.asyncio
async def test_role_reconnects_without_leaking_transport_errors(
    tmp_path, monkeypatch, caplog
):
    from medical_rag import worker
    from medical_rag.config import Settings

    attempts = []

    class Transport:
        def __init__(self, url):
            pass

        async def connect(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("secret:password@host")
            raise asyncio.CancelledError

        async def close(self):
            pass

    monkeypatch.setattr(worker, "AMQPTransport", Transport)
    settings = Settings(
        chat_history_file=str(tmp_path / "history.db"),
        memory_episodes_dir=str(tmp_path / "episodes"),
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run_role("memory", settings)
    assert len(attempts) == 2
    assert "secret" not in caplog.text


@pytest.mark.parametrize("role", ["relay", "ingest"])
def test_main_selects_lock_only_for_ingest(tmp_path, monkeypatch, role):
    import sys

    from medical_rag import worker

    events = []

    class Lock:
        def __init__(self, path):
            events.append("lock")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append("unlocked")

    async def run(role, settings):
        events.append(role)

    monkeypatch.setattr(sys, "argv", ["worker", role])
    monkeypatch.setattr(worker, "run_role", run)
    monkeypatch.setattr(worker, "IngestLock", Lock)
    worker.main()
    assert events == (["lock", "ingest", "unlocked"] if role == "ingest" else ["relay"])


@pytest.mark.asyncio
async def test_periodic_heartbeat_recovers_and_shutdown_preserves_cancellation(
    tmp_path, monkeypatch, caplog
):
    from medical_rag import worker
    from medical_rag.config import Settings
    from medical_rag.types import ApiError

    recovered = asyncio.Event()
    heartbeats = []
    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await original_sleep(0)

    def heartbeat(store, role, broker="unknown"):
        heartbeats.append(broker)
        if len(heartbeats) == 1 or broker == "stopped":
            raise ApiError("JOB_STORAGE_ERROR", "secret local path", 503)
        recovered.set()

    class Transport:
        def __init__(self, url):
            pass

        async def connect(self):
            await recovered.wait()
            raise asyncio.CancelledError("original cancellation")

        async def close(self):
            raise OSError("secret close failure")

    monkeypatch.setattr(worker.DocumentJobStore, "heartbeat", heartbeat)
    monkeypatch.setattr(worker, "AMQPTransport", Transport)
    monkeypatch.setattr(worker.asyncio, "sleep", fast_sleep)
    settings = Settings(
        chat_history_file=str(tmp_path / "history.db"),
        memory_episodes_dir=str(tmp_path / "episodes"),
    )
    with pytest.raises(asyncio.CancelledError, match="original cancellation"):
        await asyncio.wait_for(worker.run_role("memory", settings), timeout=0.5)
    assert len(heartbeats) >= 3 and heartbeats[-1] == "stopped"
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_reconnect_storage_and_close_failures_do_not_exit_runtime(
    tmp_path, monkeypatch
):
    from medical_rag import worker
    from medical_rag.config import Settings
    from medical_rag.types import ApiError

    attempts = []

    def broken_heartbeat(*args):
        raise ApiError("JOB_STORAGE_ERROR", "temporary storage unavailable", 503)

    class Transport:
        def __init__(self, url):
            pass

        async def connect(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("temporary broker unavailable")
            raise asyncio.CancelledError("stop after reconnect")

        async def close(self):
            raise OSError("close unavailable")

    monkeypatch.setattr(worker.DocumentJobStore, "heartbeat", broken_heartbeat)
    monkeypatch.setattr(worker, "AMQPTransport", Transport)
    settings = Settings(
        chat_history_file=str(tmp_path / "history.db"),
        memory_episodes_dir=str(tmp_path / "episodes"),
    )
    with pytest.raises(asyncio.CancelledError, match="stop after reconnect"):
        await worker.run_role("memory", settings)
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_relay_maintenance_storage_failure_does_not_block_publication(
    tmp_path, monkeypatch
):
    from medical_rag import worker
    from medical_rag.config import Settings
    from medical_rag.types import ApiError

    events = []

    def cleanup(*args):
        events.append("maintenance")
        raise ApiError("JOB_STORAGE_ERROR", "maintenance temporarily unavailable", 503)

    class Transport:
        def __init__(self, url):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

    class Relay:
        def __init__(self, *args):
            self.publish_failed = False

        def recover_expired(self):
            pass

        async def process_once(self):
            events.append("published")
            raise asyncio.CancelledError("stop after publication")

    monkeypatch.setattr(worker, "AMQPTransport", Transport)
    monkeypatch.setattr(worker, "OutboxRelay", Relay)
    monkeypatch.setattr(worker.DocumentJobStore, "cleanup_orphans", cleanup)
    settings = Settings(
        chat_history_file=str(tmp_path / "history.db"),
        memory_episodes_dir=str(tmp_path / "episodes"),
    )
    with pytest.raises(asyncio.CancelledError, match="stop after publication"):
        await asyncio.wait_for(worker.run_role("relay", settings), timeout=0.2)
    assert events == ["maintenance", "published"]
