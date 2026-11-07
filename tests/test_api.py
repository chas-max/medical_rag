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


def test_answers_can_skip_legacy_memory(monkeypatch):
    calls = []

    class CaptureService(FakeService):
        def answer(self, question, provider, top_k, **kwargs):
            calls.append(kwargs)
            return super().answer(question, provider, top_k)

    monkeypatch.setattr("medical_rag.api.MemoryStore.snapshot",
                        lambda self, question: (1, "private-legacy-context"))
    api = TestClient(create_app(service=CaptureService()))
    assert api.post("/answers", json={"question": "x", "memory": False}).status_code == 200
    assert calls == [{"dialogue": None, "retain_run": False}]
    assert api.post("/answers", json={"question": "x"}).status_code == 200
    assert calls[-1] == {"dialogue": None, "retain_run": True,
                         "memory_context": "private-legacy-context"}


def test_any_provider_label_reaches_the_answer_service():
    calls = []

    class CaptureService(FakeService):
        def answer(self, question, provider, top_k, **kwargs):
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

def test_context_answers_never_touch_legacy_memory(monkeypatch):
    monkeypatch.setenv("DIALOGUE_STATE_ENABLED", "true")
    calls = []

    class CaptureService(FakeService):
        def answer(self, question, provider, top_k, **kwargs):
            calls.append(kwargs)
            return {"answer": "合成回答", "citations": [], "abstained": False,
                    "answer_type": "answer"}

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy memory must not be read for a context call")

    monkeypatch.setattr("medical_rag.api.MemoryStore.snapshot", forbidden)
    api = TestClient(create_app(service=CaptureService()))
    body = {"question": "那它是由什么引起的？", "memory": False,
            "dialogue_context": {"recent": [{"turn_id": "1", "user": "糖尿病呢？",
                                             "assistant": "合成回答"}]}}

    response = api.post("/answers", json=body)

    assert response.status_code == 200
    assert calls[0]["retain_run"] is False
    assert calls[0]["dialogue"].snapshot.recent[0].turn_id == "1"


def test_context_with_legacy_memory_is_rejected(monkeypatch):
    monkeypatch.setenv("DIALOGUE_STATE_ENABLED", "true")
    api = TestClient(create_app(service=FakeService()))

    response = api.post("/answers", json={
        "question": "那它是由什么引起的？", "memory": True, "dialogue_context": {}})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_QUERY"


def test_context_without_the_feature_switch_is_refused(monkeypatch):
    monkeypatch.setenv("DIALOGUE_STATE_ENABLED", "false")
    api = TestClient(create_app(service=FakeService()))

    response = api.post("/answers", json={
        "question": "那它是由什么引起的？", "memory": False,
        "dialogue_context": {"recent": [{"turn_id": "1", "user": "糖尿病呢？"}]}})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DIALOGUE_FEATURE_DISABLED"


def test_memory_false_without_context_keeps_the_legacy_answer(monkeypatch):
    calls = []

    class CaptureService(FakeService):
        def answer(self, question, provider, top_k, **kwargs):
            calls.append(kwargs)
            return super().answer(question, provider, top_k)

    api = TestClient(create_app(service=CaptureService()))
    assert api.post("/answers", json={"question": "x", "memory": False}).status_code == 200
    assert calls == [{"dialogue": None, "retain_run": False}]
