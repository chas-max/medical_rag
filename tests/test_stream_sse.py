"""The streaming answer endpoint must publish the same run, never a new one."""

import json

import pytest
from fastapi.testclient import TestClient

from medical_rag.api import create_app
from medical_rag.types import ApiError

SECRET = "fictional-upstream-token-NEVER-RETURN"


def read_events(body):
    events = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


class StreamingService:
    """Emits what the drafting node emits, then returns the authoritative answer."""

    def __init__(self, events=(), answer=None, error=None):
        self.events = list(events)
        self.answer_result = answer or {"answer": "结论一", "citations": [],
                                        "abstained": False, "answer_type": "answer"}
        self.error = error
        self.calls = []

    def answer(self, question, provider, top_k, **kwargs):
        self.calls.append(kwargs)
        emit = kwargs.get("emit")
        if callable(emit):
            for event in self.events:
                emit(event)
        if self.error is not None:
            raise self.error
        return self.answer_result


def test_stream_frames_precede_the_authoritative_answer():
    service = StreamingService(events=[
        {"event": "status", "data": {"stage": "generating", "evidence": 2}},
        {"event": "claim", "data": {"text": "结论一", "citations": []}},
    ])
    api = TestClient(create_app(service=service))

    response = api.post("/answers/stream", json={"question": "高血压", "memory": False})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert read_events(response.text) == [
        ("status", {"stage": "generating", "evidence": 2}),
        ("claim", {"text": "结论一", "citations": []}),
        ("done", {"answer": "结论一", "citations": [], "abstained": False,
                  "answer_type": "answer"}),
    ]


def test_stream_done_payload_equals_the_plain_answer_payload():
    service = StreamingService()
    api = TestClient(create_app(service=service))

    plain = api.post("/answers", json={"question": "高血压", "memory": False}).json()["data"]
    streamed = read_events(
        api.post("/answers/stream", json={"question": "高血压", "memory": False}).text)
    assert streamed[-1] == ("done", plain)


def test_stream_and_plain_receive_the_same_run_options():
    service = StreamingService()
    api = TestClient(create_app(service=service))

    api.post("/answers", json={"question": "高血压", "memory": False})
    plain = dict(service.calls[-1])
    api.post("/answers/stream", json={"question": "高血压", "memory": False})
    streamed = dict(service.calls[-1])

    assert streamed.pop("emit") is not None
    assert streamed == plain


@pytest.mark.parametrize("event", [
    {"event": "done", "data": {"answer": SECRET}},
    {"event": "error", "data": {"code": SECRET}},
    {"event": "node.progress", "data": {"stage": SECRET}},
])
def test_nodes_cannot_forge_terminal_or_foreign_events(event):
    api = TestClient(create_app(service=StreamingService(events=[event])))

    events = read_events(api.post("/answers/stream", json={"question": "q"}).text)

    assert [name for name, _ in events] == ["error"]
    assert SECRET not in json.dumps(events, ensure_ascii=False)


def test_run_failure_becomes_a_catalogue_error_without_upstream_text():
    api = TestClient(create_app(
        service=StreamingService(error=ApiError("MODEL_RATE_LIMITED", SECRET, 429))))

    events = read_events(api.post("/answers/stream", json={"question": "q"}).text)

    assert events == [("error", {"code": "MODEL_RATE_LIMITED",
                                 "message": "生成模型请求过于频繁，请稍后重试",
                                 "status": 429})]
    assert SECRET not in json.dumps(events, ensure_ascii=False)


def test_unknown_failure_uses_the_static_dependency_error():
    api = TestClient(create_app(service=StreamingService(error=RuntimeError(SECRET))))

    events = read_events(api.post("/answers/stream", json={"question": "q"}).text)

    assert events == [("error", {"code": "DEPENDENCY_ERROR",
                                 "message": "聊天服务暂不可用", "status": 503})]


def test_request_errors_stay_json_and_never_start_a_stream(monkeypatch):
    monkeypatch.setenv("DIALOGUE_STATE_ENABLED", "true")
    api = TestClient(create_app(service=StreamingService()))

    response = api.post("/answers/stream", json={
        "question": "那它是由什么引起的？", "memory": True, "dialogue_context": {}})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "INVALID_QUERY"


def test_locked_mode_closes_the_stream_route(monkeypatch):
    monkeypatch.setenv("MEDICAL_RAG_ACCESS_MODE", "locked")
    api = TestClient(create_app(service=StreamingService()))

    response = api.post("/answers/stream", json={"question": "q"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SECURITY_NOT_READY"
