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
    assert response.json() == {"success": True, "data": {"milvus": True}, "error": None}


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


def test_unknown_provider_is_rejected():
    response = TestClient(create_app(service=FakeService())).post(
        "/answers", json={"question": "什么是高血压？", "provider": "other"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_default_answer_uses_qwen_and_deepseek_is_rejected():
    calls = []

    class CaptureService(FakeService):
        def answer(self, question, provider, top_k):
            calls.append(provider)
            return super().answer(question, provider, top_k)

    api = TestClient(create_app(service=CaptureService()))
    assert api.post("/answers", json={"question": "高血压"}).status_code == 200
    assert calls == ["qwen"]
    rejected = api.post("/answers", json={"question": "高血压", "provider": "deepseek"})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"


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
