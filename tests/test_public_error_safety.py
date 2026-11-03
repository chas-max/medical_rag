"""Untrusted exception data must not reach public responses or stored history."""

import json
import sqlite3

import pytest
from fastapi import HTTPException
from test_memory_api import memory_client
from test_profile_chat_api import StaticAnalyzer
from test_stream_api import read_events
from test_workflow_engine import FunctionalNode, make_definition, make_registry

from medical_rag.profile_models import IntentDecision
from medical_rag.types import ApiError
from medical_rag.workflow.contracts import NodeResult
from medical_rag.workflow.engine import WorkflowEngine

SECRET = "fictional-secret-token-DO-NOT-RETURN"
PRIVATE = "fictional-private-medical-record-DO-NOT-RETURN"
UNKNOWN_CODE = "UPSTREAM_" + SECRET


def assert_no_private(value):
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    assert SECRET not in serialized
    assert PRIVATE not in serialized
    assert UNKNOWN_CODE not in serialized
    assert "Traceback" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "expected_code", "message"), [
    (UNKNOWN_CODE, "DEPENDENCY_ERROR", "聊天服务暂不可用"),
    ([SECRET], "DEPENDENCY_ERROR", "聊天服务暂不可用"),
    ("NOT_READY", "NOT_READY", "服务尚未初始化"),
    ("MODEL_RATE_LIMITED", "MODEL_RATE_LIMITED", "生成模型请求过于频繁，请稍后重试"),
])
async def test_workflow_api_error_uses_fixed_public_trace(code, expected_code, message):
    def fail(inputs, context):
        raise ApiError(code, SECRET + PRIVATE, 599)

    node = FunctionalNode("test.error", "failure", "output", function=fail)
    result = await WorkflowEngine(make_registry(node)).run(
        make_definition([{"id": "node", "type": "test.error"}], []), {})
    assert result.trace[-1].error == {"code": expected_code, "message": message}
    assert_no_private(result.trace)


@pytest.mark.asyncio
async def test_failed_node_does_not_copy_untrusted_metadata():
    def fail(inputs, context):
        return NodeResult(outputs={}, status="failed", error={"code": SECRET, "message": PRIVATE},
                          metadata={"private": SECRET, "nested": {"data": PRIVATE}})

    node = FunctionalNode("test.error", "failure", "output", function=fail)
    result = await WorkflowEngine(make_registry(node)).run(
        make_definition([{"id": "node", "type": "test.error"}], []), {})
    assert result.trace[-1].metadata == {}
    assert_no_private(result.trace)


@pytest.mark.parametrize("error", [
    ApiError(UNKNOWN_CODE, SECRET + PRIVATE, 599),
    ApiError("MODEL_RATE_LIMITED", SECRET + PRIVATE, 599),
])
def test_chat_failure_is_safe_in_json_sse_and_all_history_tables(tmp_path, monkeypatch, error):
    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=StaticAnalyzer(IntentDecision(needs_answer=True)))

    def fail_service():
        raise error

    app.state.chat_runtime.service_factory = fail_service
    payload = {"request_id": "r-json", "conversation_id": "c1", "inputs": {"question": "普通问题"}}
    plain = api.post("/chat/runs", json=payload)
    assert plain.status_code == 200
    assert plain.json()["data"]["error"]["code"] == (
        "MODEL_RATE_LIMITED" if error.code == "MODEL_RATE_LIMITED" else "DEPENDENCY_ERROR")
    assert_no_private(plain.json())
    payload["request_id"] = "r-sse"
    streamed = api.post("/chat/runs/stream", json=payload)
    events = read_events(streamed.text)
    assert events[-1][0] == "done"
    assert_no_private(events)
    assert_no_private(api.get("/conversations/c1").json())
    with sqlite3.connect(tmp_path / "history.sqlite3") as connection:
        for table in ("runs", "turns", "chat_requests"):
            assert_no_private(connection.execute(f"SELECT * FROM {table}").fetchall())


@pytest.mark.parametrize(("error", "expected_status", "expected_code"), [
    (ApiError(UNKNOWN_CODE, SECRET + PRIVATE, 599), 503, "DEPENDENCY_ERROR"),
    (ApiError([SECRET], SECRET + PRIVATE, 599), 503, "DEPENDENCY_ERROR"),
    (ApiError("MEMORY_CONFIRMATION_CONFLICT", SECRET + PRIVATE, 599), 409, "MEMORY_CONFIRMATION_CONFLICT"),
    (HTTPException(status_code=400, detail={"secret": SECRET, "record": PRIVATE}), 400, "HTTP_ERROR"),
    (HTTPException(status_code=418, detail=SECRET), 503, "DEPENDENCY_ERROR"),
])
def test_confirm_exception_response_is_fixed(monkeypatch, tmp_path, caplog, error, expected_status, expected_code):
    api, app, _ = memory_client(tmp_path, monkeypatch)

    def fail(payload):
        raise error

    monkeypatch.setattr(app.state.chat_runtime, "confirm", fail)
    response = api.post("/chat/confirmations", json={
        "request_id": "r", "conversation_id": "c", "token": "t", "accept": True,
    })
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert_no_private(response.json())
    assert_no_private(caplog.text)


def test_confirmation_resolution_persists_only_fixed_error_text(monkeypatch, tmp_path):
    from test_profile_chat_api import nurse_decision

    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=StaticAnalyzer(nurse_decision()))
    app.state.memory.save_profile("occupation", "教师", "background")
    original = api.post("/chat/runs", json={
        "request_id": "original", "conversation_id": "c1", "inputs": {"question": "我是一名护士"},
    }).json()["data"]

    def fail(*args, **kwargs):
        raise ApiError("MEMORY_CONFIRMATION_CONFLICT", SECRET + PRIVATE, 599)

    monkeypatch.setattr(app.state.chat_runtime.profiles, "confirm", fail)
    response = api.post("/chat/confirmations", json={
        "request_id": "r", "conversation_id": "c1", "token": original["profile"]["confirmation_token"],
        "accept": True,
    })
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "画像已变更，请重新发起确认"
    assert_no_private(response.json())
    assert_no_private(api.get("/conversations/c1").json())
    assert_no_private(api.get("/runs/" + original["run_id"]).json())
    with sqlite3.connect(tmp_path / "history.sqlite3") as connection:
        for table in ("runs", "turns", "chat_requests"):
            assert_no_private(connection.execute(f"SELECT * FROM {table}").fetchall())
