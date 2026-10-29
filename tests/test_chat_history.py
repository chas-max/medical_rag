"""History must survive a new app/service, not just a React re-render."""

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
from medical_rag.types import ApiError


def history_client(tmp_path, monkeypatch, transport=None):
    monkeypatch.setenv("CHAT_HISTORY_FILE", str(tmp_path / "history.sqlite3"))
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(transport or StreamTransport()),
                         workflow_root=tmp_path / "workflows")
    from medical_rag.profile_models import IntentDecision

    class QueryAnalyzer:
        def analyze(self, question, snapshot):
            return IntentDecision(needs_answer=True)

    return TestClient(create_app(service=service, profile_analyzer=QueryAnalyzer())), service


@pytest.mark.parametrize("stream", [False, True])
def test_complete_history_and_run_survive_application_restart(tmp_path, monkeypatch, stream):
    from test_stream_api import answer_document_with_analysis, plan_reply

    class WithAnalysis(StreamTransport):
        def complete(self, provider, system, user):
            return (answer_document_with_analysis(user) if "evidence" in json.loads(user)
                    else plan_reply())

        def stream_chat(self, provider, messages, tools=None, on_content=None):
            text = answer_document_with_analysis(messages[-1]["content"])
            if on_content:
                on_content(text)
            return {"content": text, "tool_calls": []}

    api, _ = history_client(tmp_path, monkeypatch, WithAnalysis())
    upload_and_process(api, files={"file": ("guide.md", "# 指南\n\n如何随访？应规律随访。".encode())})
    route = "/chat/runs" + ("/stream" if stream else "")
    response = api.post(route, json={"request_id": "request-1", "conversation_id": "chat-1", "inputs": {
        "question": "如何随访？", "provider": "qwen", "top_k": 5}})
    assert response.status_code == 200, response.text
    run = read_events(response.text)[-1][1] if stream else response.json()["data"]
    assert run["status"] == "success"
    assert run["output"]["citations"]

    restarted, _ = history_client(tmp_path, monkeypatch)
    history = restarted.get("/conversations").json()["data"]
    assert len(history) == 1
    assert history[0]["id"] == "chat-1"
    question, answer = history[0]["messages"]
    assert question == {"role": "user", "content": "如何随访？"}
    assert answer["content"] == run["output"]["answer"]
    assert answer["citations"] == run["output"]["citations"]
    assert answer["analysis"] == run["output"]["analysis"]
    assert answer["runId"] == run["run_id"]
    assert "pending" not in answer
    assert restarted.get("/conversations/chat-1").json()["data"] == history[0]
    assert restarted.get(f"/runs/{run['run_id']}").json()["data"] == run


def test_question_is_committed_before_workflow_starts(tmp_path, monkeypatch):
    api, service = history_client(tmp_path, monkeypatch)
    original = service.run_workflow

    async def inspect(*args, **kwargs):
        another, _ = history_client(tmp_path, monkeypatch)
        messages = another.get("/conversations/chat-pending").json()["data"]["messages"]
        assert messages == [{"role": "user", "content": "等待中的问题"}]
        return await original(*args, **kwargs)

    service.run_workflow = inspect
    response = api.post("/chat/runs/stream", json={
        "request_id": "request-1", "conversation_id": "chat-pending", "inputs": {"question": "等待中的问题"}})
    assert read_events(response.text)[-1][0] == "done"


def test_failure_does_not_erase_question_or_create_partial_answer(tmp_path, monkeypatch):
    api, service = history_client(tmp_path, monkeypatch)

    async def broken(*args, emit=None, **kwargs):
        if emit:
            emit({"event": "claim", "data": {"text": "半截答案", "citations": []}})
        raise ApiError("DEPENDENCY_ERROR", "测试错误", 503)

    service.run_workflow = broken
    response = api.post("/chat/runs/stream", json={
        "request_id": "request-1", "conversation_id": "failed", "inputs": {"question": "失败的问题"}})
    terminal = read_events(response.text)[-1]
    assert terminal[0] == "done"
    assert terminal[1]["status"] == "failed"
    assert terminal[1]["error"]["code"] == "DEPENDENCY_ERROR"
    restarted, _ = history_client(tmp_path, monkeypatch)
    messages = restarted.get("/conversations/failed").json()["data"]["messages"]
    assert messages[0] == {"role": "user", "content": "失败的问题"}
    assert messages[1]["content"] == ""
    assert messages[1]["citations"] == []
    assert messages[1]["error"]["code"] == "DEPENDENCY_ERROR"


def test_follow_up_turns_survive_restart(tmp_path, monkeypatch):
    api, _ = history_client(tmp_path, monkeypatch)
    for question in ["第一个问题", "第二个问题"]:
        result = api.post("/chat/runs", json={
            "request_id": question, "conversation_id": "followup", "inputs": {"question": question}})
        assert result.status_code == 200
    restarted, _ = history_client(tmp_path, monkeypatch)
    messages = restarted.get("/conversations/followup").json()["data"]["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"] * 2
    assert [message["content"] for message in messages[::2]] == ["第一个问题", "第二个问题"]
    assert restarted.get("/conversations/missing").status_code == 404


def test_legacy_import_is_idempotent_and_never_overwrites_server_answers(tmp_path, monkeypatch):
    api, _ = history_client(tmp_path, monkeypatch)
    legacy = [{"id": "legacy", "title": "旧问题", "messages": [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧答案", "citations": [], "pending": True}]}]
    first = api.post("/conversations/import", json={"conversations": legacy})
    assert first.status_code == 200, first.text
    assert "pending" not in first.json()["data"][0]["messages"][1]
    stale = [{**legacy[0], "messages": legacy[0]["messages"][:1]}]
    api.post("/conversations/import", json={"conversations": stale})
    restarted, _ = history_client(tmp_path, monkeypatch)
    history = restarted.get("/conversations").json()["data"]
    assert len(history) == 1
    assert history[0]["messages"][1]["content"] == "旧答案"
    assert api.post("/conversations/import", json={"conversations": [
        {"id": "bad", "title": "bad", "messages": [{"role": "system", "content": "bad"}]}]
    }).status_code == 422


def test_failed_result_keeps_question_and_trace_after_restart(tmp_path, monkeypatch):
    from medical_rag.workflow.contracts import NodeTrace
    from medical_rag.workflow.engine import WorkflowRunResult

    api, service = history_client(tmp_path, monkeypatch)

    async def failed(*args, **kwargs):
        return WorkflowRunResult(kwargs["run_id"], "failed", {}, [
            NodeTrace("agent", "failed", error={"code": "TIMEOUT", "message": "测试超时"})])

    service.run_workflow = failed
    result = api.post("/chat/runs", json={
        "request_id": "request-1", "conversation_id": "failed-run-chat", "inputs": {"question": "失败的问题"}})
    assert result.json()["data"]["status"] == "failed"
    restarted, _ = history_client(tmp_path, monkeypatch)
    saved = restarted.get("/conversations/failed-run-chat").json()["data"]
    assert saved["messages"] == [{"role": "user", "content": "失败的问题"}]
    assert saved["failedRuns"][0]["runId"] == result.json()["data"]["run_id"]
    assert saved["failedRuns"][0]["trace"][0]["error"]["code"] == "TIMEOUT"


def test_unwritable_history_reports_storage_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_HISTORY_FILE", str(tmp_path))  # A directory, not a database.
    api = TestClient(create_app(service=object()), raise_server_exceptions=False)
    response = api.get("/conversations")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "HISTORY_STORAGE_ERROR"


def test_overlapping_turns_finish_in_their_original_question_order(tmp_path):
    from medical_rag.chat_history import ChatHistoryStore

    store = ChatHistoryStore(tmp_path / "overlap.sqlite3")
    first = store.begin_turn("overlap", "第一个问题")
    second = store.begin_turn("overlap", "第二个问题")
    for turn_id, answer in [(second, "第二个答案"), (first, "第一个答案")]:
        store.finish_turn("overlap", turn_id, {
            "run_id": f"run-{turn_id}", "status": "success", "output": {"answer": answer}, "trace": []})
    reopened = ChatHistoryStore(store.path)
    assert [message["content"] for message in reopened.get_conversation("overlap")["messages"]] == [
        "第一个问题", "第一个答案", "第二个问题", "第二个答案"]


def test_saved_abstention_keeps_its_reason(tmp_path):
    """历史重开后徽标仍要能区分“资料不足”与“生成异常”，原因码必须一起落库。"""
    from medical_rag.chat_history import ChatHistoryStore

    store = ChatHistoryStore(tmp_path / "abstain.sqlite3")
    turn = store.begin_turn("chat", "高血压如何随访？")
    store.finish_turn("chat", turn, {
        "run_id": "run-parse-error", "status": "success", "trace": [],
        "output": {"answer": "回答生成异常", "citations": [], "abstained": True,
                   "abstain_reason": "ABSTAIN_PARSE_ERROR"}})

    message = ChatHistoryStore(store.path).get_conversation("chat")["messages"][1]
    assert message["abstained"] is True
    assert message["abstainReason"] == "ABSTAIN_PARSE_ERROR"
