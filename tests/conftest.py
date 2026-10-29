"""Keep API test history isolated from the application's persistent data."""

from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def isolated_chat_history(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_FILE", str(tmp_path / "chat-history.sqlite3"))
    monkeypatch.setenv("MEMORY_EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DOCUMENT_UPLOADS_DIR", str(tmp_path / "uploads"))


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
