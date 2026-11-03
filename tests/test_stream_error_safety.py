"""The real SSE endpoint must not expose untrusted exception details."""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from medical_rag.api import create_app
from medical_rag.types import ApiError

FAKE_TOKEN = "fictional-token-NEVER-RETURN"
FAKE_RECORD = "虚构病历片段-NEVER-RETURN"
DEFAULT = {"code": "DEPENDENCY_ERROR", "message": "聊天服务暂不可用", "status": 503}


def read_events(body):
    events = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def stream_with(monkeypatch, run):
    app = create_app(service=object(), profile_analyzer=object())
    monkeypatch.setattr(app.state.chat_runtime, "run", run)
    return TestClient(app).post("/chat/runs/stream", json={
        "request_id": "request-1", "conversation_id": "conversation-1",
        "inputs": {"question": "示例问题"},
    })


@pytest.mark.parametrize("exception", [
    ApiError(FAKE_TOKEN, FAKE_RECORD, 418),
    ApiError("UPSTREAM_" + FAKE_TOKEN, "上下文:" + FAKE_RECORD, 500),
    ApiError("DEPENDENCY_ERROR", FAKE_TOKEN + FAKE_RECORD, 599),
    ApiError([FAKE_TOKEN], FAKE_RECORD, 418),
    RuntimeError(FAKE_TOKEN + FAKE_RECORD),
])
def test_unknown_errors_are_static_and_terminal(monkeypatch, caplog, exception):
    def run(payload, emit):
        emit({"event": "chat.progress", "data": {"stage": "intent"}})
        raise exception

    response = stream_with(monkeypatch, run)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert read_events(response.text) == [("chat.progress", {"stage": "intent"}), ("error", DEFAULT)]
    for secret in (FAKE_TOKEN, FAKE_RECORD):
        assert secret not in response.text
        assert secret not in caplog.text


@pytest.mark.parametrize(("code", "public_message"), [
    ("REQUEST_ID_CONFLICT", "请求标识已用于不同内容"),
    ("REQUEST_IN_PROGRESS", "相同请求仍在处理中，请稍后重试"),
])
def test_known_business_errors_use_fixed_public_message_and_status(monkeypatch, caplog, code, public_message):
    def run(payload, emit):
        raise ApiError(code, FAKE_TOKEN + FAKE_RECORD, 599)

    response = stream_with(monkeypatch, run)
    assert read_events(response.text) == [("error", {"code": code, "message": public_message, "status": 409})]
    assert FAKE_TOKEN not in response.text and FAKE_RECORD not in response.text
    assert FAKE_TOKEN not in caplog.text and FAKE_RECORD not in caplog.text


@pytest.mark.parametrize("data", [
    {"code": "REQUEST_ID_CONFLICT", "message": FAKE_TOKEN + FAKE_RECORD, "status": 409},
    {"code": FAKE_TOKEN, "message": FAKE_RECORD, "status": 503},
    FAKE_TOKEN + FAKE_RECORD,
    None,
])
def test_emitter_error_event_is_sanitized_and_terminates_stream(monkeypatch, caplog, data):
    def run(payload, emit):
        emit({"event": "chat.progress", "data": {"stage": "intent"}})
        emit({"event": "error", "data": data})
        emit({"event": "chat.progress", "data": {"stage": "after-error"}})
        return {"status": "success"}

    response = stream_with(monkeypatch, run)
    assert read_events(response.text) == [("chat.progress", {"stage": "intent"}), ("error", DEFAULT)]
    assert FAKE_TOKEN not in response.text and FAKE_RECORD not in response.text
    assert FAKE_TOKEN not in caplog.text and FAKE_RECORD not in caplog.text


@pytest.mark.parametrize("exception", [ApiError("REQUEST_ID_CONFLICT", FAKE_RECORD, 409), RuntimeError(FAKE_TOKEN)])
def test_emitter_error_followed_by_exception_does_not_emit_a_second_error(monkeypatch, exception):
    def run(payload, emit):
        emit({"event": "error", "data": FAKE_RECORD})
        raise exception

    assert read_events(stream_with(monkeypatch, run).text) == [("error", DEFAULT)]


@pytest.mark.parametrize("mutate_event_name", [True, False])
def test_emitter_snapshots_event_and_nested_data_before_upstream_mutation(monkeypatch, mutate_event_name):
    from medical_rag import api as api_module

    ready = threading.Event()
    original_frame = api_module._event_frame

    def wait_until_mutated(event):
        assert ready.wait(timeout=5)
        return original_frame(event)

    monkeypatch.setattr(api_module, "_event_frame", wait_until_mutated)

    def run(payload, emit):
        event = {"event": "chat.progress", "data": {"stage": "intent", "nested": {"value": "before"}}}
        emit(event)
        if mutate_event_name:
            event["event"] = "error"
        event["data"]["nested"]["value"] = FAKE_TOKEN + FAKE_RECORD
        ready.set()
        return {"status": "success"}

    response = stream_with(monkeypatch, run)
    assert read_events(response.text) == [
        ("chat.progress", {"stage": "intent", "nested": {"value": "before"}}),
        ("done", {"status": "success"}),
    ]
    assert FAKE_TOKEN not in response.text and FAKE_RECORD not in response.text


@pytest.mark.parametrize("kind", ["done", "unknown", "newline", "nondict", "unserializable", "cyclic", "deep", "nan"])
def test_invalid_upstream_events_are_fixed_terminal_errors(monkeypatch, caplog, kind):
    def run(payload, emit):
        if kind == "done":
            event = {"event": "done", "data": {"secret": FAKE_TOKEN}}
        elif kind == "unknown":
            event = {"event": "custom", "data": FAKE_RECORD}
        elif kind == "newline":
            event = {"event": "status\nevent: error", "data": FAKE_TOKEN}
        elif kind == "nondict":
            event = [FAKE_TOKEN, FAKE_RECORD]
        elif kind == "unserializable":
            event = {"event": "status", "data": {"secret": FAKE_TOKEN, "bad": object()}}
        elif kind == "cyclic":
            event = {"event": "status", "data": [FAKE_TOKEN]}
            event["data"].append(event)
        elif kind == "deep":
            data = [FAKE_TOKEN]
            for _ in range(1500):
                data = [data]
            event = {"event": "status", "data": data}
        else:
            event = {"event": "status", "data": float("nan")}
        emit(event)
        emit({"event": "chat.progress", "data": {"stage": "after-error"}})
        return {"status": "success"}

    response = stream_with(monkeypatch, run)
    assert read_events(response.text) == [("error", DEFAULT)]
    assert FAKE_TOKEN not in response.text and FAKE_RECORD not in response.text
    assert FAKE_TOKEN not in caplog.text and FAKE_RECORD not in caplog.text


def test_valid_claim_and_analysis_content_is_streamed_but_not_logged(monkeypatch, caplog):
    def run(payload, emit):
        emit({"event": "claim", "data": {"text": FAKE_RECORD, "citations": []}})
        emit({"event": "analysis", "data": {"summary": FAKE_RECORD}})
        return {"status": "success"}

    response = stream_with(monkeypatch, run)
    assert read_events(response.text) == [
        ("claim", {"text": FAKE_RECORD, "citations": []}),
        ("analysis", {"summary": FAKE_RECORD}),
        ("done", {"status": "success"}),
    ]
    assert FAKE_RECORD not in caplog.text


def test_normal_stream_still_emits_progress_and_done(monkeypatch):
    def run(payload, emit):
        emit({"event": "chat.progress", "data": {"stage": "intent"}})
        return {"status": "success", "value": "公开结果"}

    assert read_events(stream_with(monkeypatch, run).text) == [
        ("chat.progress", {"stage": "intent"}), ("done", {"status": "success", "value": "公开结果"}),
    ]
