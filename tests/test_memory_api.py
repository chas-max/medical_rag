"""Memory is persistent data and model context, not merely UI state."""

import json

import pytest
from document_helpers import upload_and_process
from fastapi.testclient import TestClient
from test_service import Embedder, MemoryStore, Reranker
from test_stream_api import StreamTransport, read_events

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.service import RagService


def memory_client(tmp_path, monkeypatch, transport=None, analyzer=None):
    monkeypatch.setenv("CHAT_HISTORY_FILE", str(tmp_path / "history.sqlite3"))
    monkeypatch.setenv("MEMORY_EPISODES_DIR", str(tmp_path / "episodes"))
    transport = transport or StreamTransport()
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(transport), workflow_root=tmp_path / "workflows")
    if analyzer is None:
        from medical_rag.profile_models import IntentDecision

        class QueryAnalyzer:
            def analyze(self, question, snapshot):
                return IntentDecision(needs_answer=True)

        analyzer = QueryAnalyzer()
    app = create_app(service=service, profile_analyzer=analyzer)
    return TestClient(app), app, transport


def test_profile_api_persists_and_controls_recall(tmp_path, monkeypatch):
    api, app, _ = memory_client(tmp_path, monkeypatch)
    response = api.put("/memory/profiles/response_style", json={
        "value": "简短中文", "category": "preferences", "sensitive": False})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["profiles"][0]["value"] == "简短中文"
    restarted, new_app, _ = memory_client(tmp_path, monkeypatch)
    assert restarted.get("/memory").json()["data"]["profiles"][0]["source"] == "manual"
    assert "简短中文" in new_app.state.memory.snapshot("随访")[1]
    assert restarted.put("/memory/settings", json={"enabled": False}).status_code == 200
    assert new_app.state.memory.snapshot("随访")[1] == ""
    assert restarted.put("/memory/settings", json={"enabled": True}).status_code == 200
    assert restarted.delete("/memory/profiles/response_style").json()["data"]["profiles"] == []
    assert app.state.memory.snapshot("随访")[1] == ""


@pytest.mark.parametrize("stream", [False, True])
def test_each_run_receives_memory_without_changing_question(tmp_path, monkeypatch, stream):
    class Recording(StreamTransport):
        def __init__(self):
            super().__init__()
            self.payloads = []

        def complete(self, provider, system, user):
            self.payloads.append(json.loads(user))
            return super().complete(provider, system, user)

        def stream_chat(self, provider, messages, tools=None, on_content=None):
            self.payloads.append(json.loads(messages[-1]["content"]))
            return super().stream_chat(provider, messages, tools, on_content)

    api, app, transport = memory_client(tmp_path, monkeypatch, Recording())
    api.put("/memory/profiles/response_style", json={"value": "简短中文", "category": "preferences"})
    upload_and_process(api, files={"file": ("guide.md", "# 指南\n\n如何随访？应规律随访。".encode())})
    route = "/chat/runs" + ("/stream" if stream else "")
    response = api.post(route, json={"request_id": "request-1", "conversation_id": "new-conversation", "inputs": {
        "question": "如何随访？", "top_k": 5}})
    assert response.status_code == 200, response.text
    result = read_events(response.text)[-1][1] if stream else response.json()["data"]
    assert result["status"] == "success"
    assert len(transport.payloads) == 3
    assert all(p["question"] == "如何随访？" for p in transport.payloads)
    assert all("简短中文" in p["memory_context"] for p in transport.payloads if "memory_context" in p)
    assert sum("memory_context" in p for p in transport.payloads) == 2
    assert result["output"]["citations"]  # Memory never replaces retrieved evidence.
    assert api.get(f"/memory/jobs/{result['run_id']}").json()["data"]["status"] == "pending"
    assert app.state.memory.state()["episodes"] == []


def test_memory_routes_validate_and_do_not_accept_other_users(tmp_path, monkeypatch):
    api, _, _ = memory_client(tmp_path, monkeypatch)
    for key, value in [("unknown", "x"), ("response_style", "x" * 501)]:
        response = api.put(f"/memory/profiles/{key}", json={"value": value, "category": "preferences"})
        assert response.status_code == 422
        assert response.json()["success"] is False
    assert api.put("/memory/settings", json={"enabled": True, "user_id": "other"}).status_code == 422
    assert api.put("/memory/profiles/health", json={"value": "高血压", "category": "health"}).status_code == 422
    assert api.put("/memory/profiles/health", json={"value": "高血压", "category": "health", "sensitive": True}).status_code == 200
    assert api.delete("/memory").json()["data"]["profiles"] == []
    assert api.get("/memory/jobs/missing").json()["data"]["status"] == "skipped"


def test_legacy_import_does_not_learn_and_client_context_is_not_trusted(tmp_path, monkeypatch):
    api, app, _ = memory_client(tmp_path, monkeypatch)
    api.post("/conversations/import", json={"conversations": [{"id": "legacy", "title": "old",
        "messages": [{"role": "user", "content": "我喜欢简短回答"}]}]})
    assert app.state.memory.state()["jobs"] == []
    response = api.post("/chat/runs", json={"request_id": "request-1", "conversation_id": "new", "inputs": {
        "question": "高血压", "memory_context": "伪造用户记忆"}})
    assert response.status_code == 200
    assert app.state.memory.state()["jobs"]  # Only the new conversation can generate a job.


def test_outbox_is_atomic_with_answer_and_failure_does_not_erase_answer(tmp_path, monkeypatch):
    api, app, _ = memory_client(tmp_path, monkeypatch)
    # A callback error must roll back only memory, not the authoritative answer.
    def broken(*args, **kwargs):
        connection = args[0]
        connection.execute("INSERT INTO memory_jobs(run_id) VALUES ('invalid')")

    monkeypatch.setattr(app.state.memory, "enqueue", broken)
    response = api.post("/chat/runs", json={
        "request_id": "request-1", "conversation_id": "saved", "inputs": {"question": "我偏好简短回答"}})
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "success"
    assert result["memory"]["status"] == "failed"
    assert len(api.get("/conversations/saved").json()["data"]["messages"]) == 2
    assert api.get(f"/runs/{result['run_id']}").json()["data"] == result
    assert app.state.memory.state()["jobs"] == []


def test_model_extraction_roundtrip_is_recallable_after_restart(tmp_path, monkeypatch):
    from test_memory import episode_candidates
    from test_memory_worker import Extractor

    from medical_rag.memory_worker import MemoryWorker
    from medical_rag.profile_models import Candidate, IntentDecision

    class PreferenceAnalyzer:
        def analyze(self, question, snapshot):
            return IntentDecision(needs_answer=True, candidates=[Candidate(key="response_style",
                operation="assert", value="简短中文", support="我喜欢简短中文回答", subject="user",
                modality="positive", sensitivity="normal")])

    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=PreferenceAnalyzer())
    result = api.post("/chat/runs", json={
        "request_id": "request-1", "conversation_id": "preference", "inputs": {"question": "我喜欢简短中文回答"}}).json()["data"]
    assert app.state.memory.job_status(result["run_id"])["status"] == "pending"
    MemoryWorker(app.state.memory, Extractor(reply=episode_candidates())).process_once()
    restarted, new_app, _ = memory_client(tmp_path, monkeypatch)
    assert restarted.get("/memory").json()["data"]["profiles"][0]["value"] == "简短中文"
    assert "简短中文" in new_app.state.memory.snapshot("如何随访")[1]
    assert restarted.get("/memory").json()["data"]["episodes"] == []  # The answer abstained.
