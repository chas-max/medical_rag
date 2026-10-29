import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from medical_rag.api import create_app
from medical_rag.chat_api import ChatRunRequest


def test_workflow_management_and_generic_execution_are_not_public():
    class Service:
        def nodes(self):
            return []

    client = TestClient(create_app(service=Service()))
    for path in ("/nodes", "/workflows", "/workflows/medical-answer"):
        assert client.get(path).status_code == 404
    assert client.post("/workflows", json={}).status_code == 404
    assert client.put("/workflows/medical-answer", json={}).status_code == 404
    assert client.post("/workflows/medical-answer/runs", json={}).status_code == 404


def test_chat_cannot_select_another_workflow():
    payload = {"request_id": "r", "conversation_id": "c", "inputs": {"question": "test"}}
    assert ChatRunRequest(**payload).workflow_id == "medical-answer"
    assert ChatRunRequest(**payload, workflow_id=None).workflow_id == "medical-answer"
    with pytest.raises(ValidationError):
        ChatRunRequest(**payload, workflow_id="medical-answer-agentic")


def test_settings_use_project_env_even_when_launched_from_another_directory(monkeypatch):
    from medical_rag.config import Settings

    monkeypatch.delenv("MILVUS_URI", raising=False)
    monkeypatch.delenv("COLLECTION_NAME", raising=False)
    monkeypatch.chdir("D:/project_rag")
    settings = Settings()
    assert settings.milvus_uri == "http://127.0.0.1:19531"
    assert settings.collection_name == "medical_rag_resume_chunks"
