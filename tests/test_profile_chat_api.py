"""End-to-end contract for the independent, mixed-intent chat entry point."""

import pytest
from document_helpers import upload_and_process
from test_memory_api import memory_client

from medical_rag.profile_models import Candidate, IntentDecision


@pytest.mark.parametrize(("question", "needs_answer"), [
    ("你记得我的职业吗", False),
    ("我是一名护士，请给出高血压管理要点", True),
    ("忘记我的职业", False),
    ("我是一名护士", False),
    ("如何随访？", True),
    ("我是护士，介绍高血压的一般知识", True),
])
def test_disabled_routing_only_shortcuts_whole_pure_memory_forms(question, needs_answer):
    from medical_rag.chat_runtime import _fallback_needs_answer
    assert _fallback_needs_answer(question) is needs_answer


class StaticAnalyzer:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def analyze(self, question, snapshot):
        self.calls += 1
        return self.decision


def nurse_decision(answer=False):
    return IntentDecision(needs_answer=answer, candidates=[Candidate(
        key="occupation", operation="assert", value="护士", support="我是一名护士",
        subject="user", modality="positive", sensitivity="normal")])


def test_memory_entry_exists_without_workflow(tmp_path, monkeypatch):
    api, _, _ = memory_client(tmp_path, monkeypatch, analyzer=StaticAnalyzer(nurse_decision()))
    response = api.post("/chat/runs", json={
        "request_id": "req-1", "conversation_id": "c-1",
        "inputs": {"question": "我是一名护士"},
        "workflow_id": None, "memory_consent": False,
    })
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["kind"] == "memory"
    assert result["trace"] == []
    assert result["episode"]["status"] == "skipped"
    assert result["profile"]["actions"][0]["status"] == "added"
    assert api.get("/memory").json()["data"]["profiles"][0]["value"] == "护士"


def test_mixed_write_precedes_unchanged_question_and_retries_are_idempotent(tmp_path, monkeypatch):
    import json

    from test_stream_api import StreamTransport

    class Recording(StreamTransport):
        def __init__(self):
            super().__init__()
            self.payloads = []

        def complete(self, provider, system, user):
            self.payloads.append(json.loads(user))
            return super().complete(provider, system, user)

    analyzer = StaticAnalyzer(nurse_decision(True))
    api, _, transport = memory_client(tmp_path, monkeypatch, Recording(), analyzer)
    upload_and_process(api, files={"file": ("guide.md", "# 指南\n\n我是一名护士。如何随访？应规律随访。".encode())})
    payload = {"request_id": "mixed1", "conversation_id": "c1", "workflow_id": "medical-answer",
               "inputs": {"question": "我是一名护士。如何随访？", "top_k": 5}}
    first = api.post("/chat/runs", json=payload)
    assert first.status_code == 200, first.text
    result = first.json()["data"]
    assert result["kind"] == "mixed"
    assert result["output"]["citations"]
    assert all(p["question"] == payload["inputs"]["question"] for p in transport.payloads)
    assert all("护士" in p["memory_context"] for p in transport.payloads if "memory_context" in p)
    assert any("memory_context" in p for p in transport.payloads)
    assert api.post("/chat/runs", json=payload).json()["data"] == result
    assert analyzer.calls == 1
    assert len(api.get("/conversations/c1").json()["data"]["messages"]) == 2
    payload["inputs"]["question"] = "changed"
    assert api.post("/chat/runs", json=payload).status_code == 409


def test_pure_memory_does_not_create_retrieval_service(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from medical_rag.api import create_app
    monkeypatch.setenv("CHAT_HISTORY_FILE", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setenv("MEMORY_EPISODES_DIR", str(tmp_path / "episodes"))
    app = create_app(profile_analyzer=StaticAnalyzer(nurse_decision()))
    with TestClient(app) as api:
        response = api.post("/chat/runs", json={"request_id": "r1", "conversation_id": "c1",
                                                "inputs": {"question": "我是一名护士"}})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["kind"] == "memory"


def payload(question="我是一名护士", request="r1", workflow=None, consent=False):
    return {"request_id": request, "conversation_id": "c1", "workflow_id": workflow,
            "inputs": {"question": question}, "memory_consent": consent}


def test_answer_dependency_failure_preserves_written_profile_and_no_fake_trace(tmp_path, monkeypatch):
    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=StaticAnalyzer(nurse_decision(True)))
    from medical_rag.types import ApiError
    def unavailable():
        raise ApiError("DEPENDENCY_ERROR", "检索不可用", 503)
    app.state.chat_runtime.service_factory = unavailable
    result = api.post("/chat/runs", json=payload(workflow="medical-answer")).json()["data"]
    assert result["kind"] == "mixed" and result["status"] == "failed"
    assert result["trace"] == [] and result["output"]["citations"] == []
    assert result["profile"]["actions"][0]["status"] == "added"
    assert result["episode"]["status"] == "skipped"
    saved = api.get("/conversations/c1").json()["data"]
    assert saved["messages"][1]["profile"] == result["profile"]
    assert saved["messages"][1]["error"] == result["error"]
    assert saved["failedRuns"][0]["runId"] == result["run_id"]
    assert api.get("/memory").json()["data"]["profiles"][0]["value"] == "护士"


def test_intent_timeout_does_not_write_or_block_question(tmp_path, monkeypatch):
    class TimeoutAnalyzer:
        def analyze(self, *args):
            raise TimeoutError("private provider text must not appear")
    api, _, _ = memory_client(tmp_path, monkeypatch, analyzer=TimeoutAnalyzer())
    response = api.post("/chat/runs", json=payload(workflow="medical-answer"))
    result = response.json()["data"]
    assert result["status"] == "success"  # Existing evidence workflow abstains safely.
    assert result["profile"]["status"] == "failed"
    assert "private provider" not in response.text
    assert api.get("/memory").json()["data"]["profiles"] == []


def test_lookup_uses_current_profiles_not_historical_episode(tmp_path, monkeypatch):
    analyzer = StaticAnalyzer(IntentDecision(needs_answer=False, lookup_keys=["occupation"]))
    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=analyzer)
    app.state.memory.save_profile("occupation", "教师", "background")
    result = api.post("/chat/runs", json=payload("我的职业是什么？")).json()["data"]
    assert result["output"]["answer"] == "职业：教师"
    app.state.memory.delete_profile("occupation")
    result = api.post("/chat/runs", json=payload("我的职业是什么？", "r2")).json()["data"]
    assert "教师" not in result["output"]["answer"]
    assert "未记录" in result["output"]["answer"]


def test_sensitive_assertion_needs_current_consent_and_deletion_does_not(tmp_path, monkeypatch):
    decision = IntentDecision(needs_answer=False, candidates=[Candidate(key="health", operation="assert",
        value="高血压", support="我患有高血压", subject="user", modality="positive", sensitivity="sensitive")])
    analyzer = StaticAnalyzer(decision)
    api, _, _ = memory_client(tmp_path, monkeypatch, analyzer=analyzer)
    blocked = api.post("/chat/runs", json=payload("我患有高血压")).json()["data"]
    assert blocked["profile"]["actions"][0]["status"] == "blocked"
    assert api.get("/memory").json()["data"]["profiles"] == []
    api.post("/chat/runs", json=payload("我患有高血压", "r2", consent=True))
    assert api.get("/memory").json()["data"]["profiles"][0]["sensitive"] is True
    analyzer.decision = IntentDecision(needs_answer=False, candidates=[Candidate(key="health",
        operation="forget", support="忘记我的健康画像", subject="user", modality="positive", sensitivity="sensitive")])
    result = api.post("/chat/runs", json=payload("忘记我的健康画像", "r3")).json()["data"]
    assert result["profile"]["actions"][0]["status"] == "deleted"
    assert api.get("/memory").json()["data"]["profiles"] == []


def test_confirmation_only_changes_bound_profile_and_resolves_original_notice(tmp_path, monkeypatch):
    analyzer = StaticAnalyzer(nurse_decision())
    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=analyzer)
    app.state.memory.save_profile("occupation", "教师", "background")
    original = api.post("/chat/runs", json=payload()).json()["data"]
    token = original["profile"]["confirmation_token"]
    assert token and app.state.memory.state()["profiles"][0]["value"] == "教师"
    confirmation = {"request_id": "yes", "conversation_id": "c1", "token": token,
                    "accept": True, "memory_consent": False}
    result = api.post("/chat/confirmations", json=confirmation).json()["data"]
    assert result["kind"] == "memory" and result["trace"] == []
    assert result["episode"]["status"] == "skipped"
    assert api.post("/chat/confirmations", json=confirmation).json()["data"] == result
    assert analyzer.calls == 1
    assert app.state.memory.state()["profiles"][0]["value"] == "护士"
    saved = api.get("/conversations/c1").json()["data"]
    assert saved["messages"][1]["profile"]["confirmation_token"] is None
    assert api.get("/runs/" + original["run_id"]).json()["data"]["profile"]["confirmation_token"] is None
    confirmation["request_id"] = "again"
    assert api.post("/chat/confirmations", json=confirmation).status_code == 409


def test_stream_progress_and_done_are_persisted_and_malformed_requests_rejected(tmp_path, monkeypatch):
    from test_stream_api import read_events
    api, _, _ = memory_client(tmp_path, monkeypatch, analyzer=StaticAnalyzer(nurse_decision()))
    response = api.post("/chat/runs/stream", json=payload())
    events = read_events(response.text)
    assert [data["stage"] for name, data in events if name == "chat.progress"] == ["intent", "profile"]
    assert events[-1][0] == "done"
    result = events[-1][1]
    assert api.get("/runs/" + result["run_id"]).json()["data"] == result
    assert api.post("/chat/runs", json={**payload(), "user_id": "other"}).status_code == 422
    assert api.post("/chat/runs", json={**payload(), "inputs": {"question": " "}}).status_code == 422
    assert api.post("/chat/runs", json={**payload(), "memory_consent": "true"}).status_code == 422


def test_disabled_memory_never_calls_analyzer(tmp_path, monkeypatch):
    analyzer = StaticAnalyzer(nurse_decision())
    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=analyzer)
    app.state.memory.set_enabled(False)
    def forbidden_service():
        raise AssertionError("Pure disabled-memory management must not retrieve")
    app.state.chat_runtime.service_factory = forbidden_service
    result = api.post("/chat/runs", json=payload("忘记我的职业", workflow="medical-answer")).json()["data"]
    assert analyzer.calls == 0
    assert result["profile"]["status"] == "disabled"
    assert result["episode"]["status"] == "skipped"
    assert "关闭" in result["output"]["answer"]
    assert result["kind"] == "memory" and result["status"] == "success"


def test_no_workflow_question_uses_fixed_default(tmp_path, monkeypatch):
    api, _, _ = memory_client(tmp_path, monkeypatch, analyzer=StaticAnalyzer(IntentDecision(needs_answer=True)))
    result = api.post("/chat/runs", json=payload("如何随访？")).json()["data"]
    assert result["status"] == "success"
    assert result["output"]["abstained"] is True


def test_expired_confirmation_error_is_terminal_in_saved_original_notice(tmp_path, monkeypatch):
    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=StaticAnalyzer(nurse_decision()))
    app.state.memory.save_profile("occupation", "教师", "background")
    original = api.post("/chat/runs", json=payload()).json()["data"]
    app.state.memory.save_profile("occupation", "医生", "background")
    request = {"request_id": "yes", "conversation_id": "c1", "token": original["profile"]["confirmation_token"],
               "accept": True, "memory_consent": False}
    for _ in range(2):
        response = api.post("/chat/confirmations", json=request)
        assert response.status_code == 409
        assert response.json()["error"]["code"] != "REQUEST_IN_PROGRESS"
    profile = api.get("/conversations/c1").json()["data"]["messages"][1]["profile"]
    assert profile["status"] == "conflict" and profile["confirmation_token"] is None


def test_optional_profile_storage_failure_does_not_break_question(tmp_path, monkeypatch):
    from medical_rag.types import ApiError
    api, app, _ = memory_client(tmp_path, monkeypatch)

    def unavailable(*args):
        raise ApiError("MEMORY_STORAGE_ERROR", "不可用", 503)

    monkeypatch.setattr(app.state.chat_runtime.profiles, "snapshot", unavailable)
    result = api.post("/chat/runs", json=payload("如何随访？", workflow="medical-answer")).json()["data"]
    assert result["status"] == "success"
    assert result["profile"]["status"] == "failed"


def test_cancelling_confirmation_leaves_manual_value_and_does_not_reask(tmp_path, monkeypatch):
    analyzer = StaticAnalyzer(nurse_decision())
    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=analyzer)
    app.state.memory.save_profile("occupation", "教师", "background")
    original = api.post("/chat/runs", json=payload()).json()["data"]
    result = api.post("/chat/confirmations", json={"request_id": "cancel", "conversation_id": "c1",
        "token": original["profile"]["confirmation_token"], "accept": False, "memory_consent": False}).json()["data"]
    assert result["profile"]["actions"][0]["status"] == "unchanged"
    assert app.state.memory.state()["profiles"][0]["value"] == "教师"
    assert analyzer.calls == 1
