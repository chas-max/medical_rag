"""Keep API test history isolated from the application's persistent data."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_chat_history(tmp_path, monkeypatch):
    # The legacy behavior tests exercise the explicitly unsafe local demo, not
    # the default security posture tested in test_security_lockdown.py.
    monkeypatch.setenv("MEDICAL_RAG_ACCESS_MODE", "unsafe-local-demo")
    monkeypatch.setenv("CHAT_HISTORY_FILE", str(tmp_path / "chat-history.sqlite3"))
    monkeypatch.setenv("MEMORY_EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DOCUMENT_UPLOADS_DIR", str(tmp_path / "uploads"))
    # The operator's .env may turn the continuous-follow-up path on for the local
    # demo. Tests pin the switch off (process env beats .env for Settings) and
    # opt in explicitly where the structured path is under test.
    monkeypatch.setenv("DIALOGUE_STATE_ENABLED", "false")
    # Starlette's synthetic default peer is not an IP address. Give legacy
    # local-demo tests a loopback peer; tests can still specify a remote peer.
    original_init = TestClient.__init__

    def local_test_client(self, *args, **kwargs):
        kwargs.setdefault("client", ("127.0.0.1", 50000))
        kwargs.setdefault("base_url", "http://127.0.0.1:8001")
        kwargs.setdefault("headers", {"Origin": "http://127.0.0.1:8001"})
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", local_test_client)


@pytest.fixture
def rabbitmq_namespace(monkeypatch):
    """Give each opt-in broker test its own exchanges and quorum queues."""
    from medical_rag import messaging

    namespace = f"test.medical_rag.{uuid4().hex}"
    monkeypatch.setattr(messaging, "TASKS_EXCHANGE", f"{namespace}.tasks")
    monkeypatch.setattr(messaging, "DEAD_EXCHANGE", f"{namespace}.dead")
    monkeypatch.setattr(
        messaging,
        "QUEUES",
        {task_type: f"{namespace}.{task_type}" for task_type in messaging.QUEUES},
    )
    monkeypatch.setattr(
        messaging,
        "DEAD_QUEUES",
        {
            task_type: f"{namespace}.{task_type}.dead"
            for task_type in messaging.DEAD_QUEUES
        },
    )
    return namespace
