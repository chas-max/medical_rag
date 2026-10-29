import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.api import create_app


class FakeService:
    def health(self):
        return {"milvus": True}

    def list_documents(self):
        return []

    def search(self, question, top_k):
        return []

    def answer(self, question, provider, top_k):
        return {"answer": "资料不足", "citations": [], "abstained": True}


def test_health_has_standard_envelope():
    response = TestClient(create_app(service=FakeService())).get("/health")
    assert response.status_code == 200
    envelope = response.json()
    assert envelope["success"] is True and envelope["error"] is None
    assert envelope["data"]["milvus"] is True
    assert "messaging" in envelope["data"]


def test_invalid_top_k_uses_error_envelope():
    response = TestClient(create_app(service=FakeService())).post(
        "/search", json={"question": "什么是高血压？", "top_k": 0}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["success"] is False


def test_unsupported_upload_is_rejected():
    response = TestClient(create_app(service=FakeService())).post(
        "/documents", files={"file": ("sample.docx", b"hello")}
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_FORMAT"


def test_any_provider_label_reaches_the_answer_service():
    calls = []

    class CaptureService(FakeService):
        def answer(self, question, provider, top_k):
            calls.append(provider)
            return super().answer(question, provider, top_k)

    api = TestClient(create_app(service=CaptureService()))
    assert api.post("/answers", json={"question": "高血压"}).status_code == 200
    assert api.post("/answers", json={"question": "高血压", "provider": "deepseek"}).status_code == 200
    assert calls == ["qwen", "deepseek"]
    malformed = api.post("/answers", json={"question": "高血压", "provider": 7})
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "VALIDATION_ERROR"


def test_default_top_k_uses_configuration(monkeypatch):
    monkeypatch.setenv("TOP_K", "3")

    class SearchService(FakeService):
        def search(self, question, top_k):
            return {"used_top_k": top_k}

    response = TestClient(create_app(service=SearchService())).post("/search", json={"question": "高血压"})
    assert response.json()["data"]["used_top_k"] == 3


def test_unknown_route_also_uses_error_envelope():
    response = TestClient(create_app(service=FakeService())).get("/nonexistent")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HTTP_ERROR"
    assert response.json()["success"] is False


def test_chat_runs_are_offloaded_from_the_event_loop(monkeypatch):
    calls = []

    async def fake_run_in_threadpool(function, *args):
        calls.append(function)
        return await asyncio.to_thread(function, *args)

    monkeypatch.setattr("medical_rag.chat_api.run_in_threadpool", fake_run_in_threadpool)
    app = create_app(service=FakeService())
    monkeypatch.setattr(app.state.chat_runtime, "run", lambda payload: {
        "run_id": "run-1", "status": "success", "output": {}, "trace": []})
    response = TestClient(app).post(
        "/chat/runs", json={"request_id": "r", "conversation_id": "c", "inputs": {"question": "高血压"}}
    )

    assert response.status_code == 200
    assert response.json()["data"]["run_id"] == "run-1"
    assert len(calls) == 1
