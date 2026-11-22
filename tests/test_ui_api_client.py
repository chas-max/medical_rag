"""Backend client: payload shape, loopback/Origin rules and SSE handling."""

import json

import httpx
import pytest

from medical_rag.ui.api_client import (
    BackendError,
    RagApiClient,
    backend_origin,
    build_chat_payload,
    iter_sse_events,
    parse_sse_block,
)


def _client(handler, **kwargs):
    return RagApiClient("http://127.0.0.1:8001", transport=httpx.MockTransport(handler),
                        **kwargs)


def _sse(*frames: tuple[str, dict]) -> bytes:
    return "".join(
        f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for name, data in frames
    ).encode("utf-8")


def test_backend_origin_requires_loopback_http():
    assert backend_origin("http://127.0.0.1:8001") == "http://127.0.0.1:8001"
    assert backend_origin("http://localhost:8001/") == "http://localhost:8001"
    for value in ("http://0.0.0.0:8001", "http://example.com", "https://10.0.0.5:8001",
                  "ftp://127.0.0.1:8001", "http://127.0.0.1:8001/api", "not-a-url"):
        with pytest.raises(ValueError):
            backend_origin(value)


def test_chat_payload_matches_frontend_contract():
    payload = build_chat_payload("  糖尿病能吃水果吗？  ", "tenant-1-abc", top_k=6,
                                 request_id="req-1")
    assert payload == {
        "request_id": "req-1",
        "conversation_id": "tenant-1-abc",
        "workflow_id": "medical-answer",
        "inputs": {"question": "糖尿病能吃水果吗？", "provider": "qwen", "top_k": 6},
        "memory_consent": False,
    }
    assert len(build_chat_payload("问题", "c")["request_id"]) == 32
    for args in (("", "c"), ("问题", ""), ("x" * 20_001, "c")):
        with pytest.raises(ValueError):
            build_chat_payload(*args)
    with pytest.raises(ValueError):
        build_chat_payload("问题", "c", top_k=0)
    with pytest.raises(ValueError):
        build_chat_payload("问题", "c", top_k=True)


def test_parse_sse_block_handles_multiline_data_and_comments():
    assert parse_sse_block(": keepalive") is None
    event, data = parse_sse_block("event: status\ndata: {\"stage\": \"retrieving\"}")
    assert event == "status"
    assert data == {"stage": "retrieving"}
    event, data = parse_sse_block("event: claim\ndata: {\"text\":\ndata: \"结论\"}")
    assert event == "claim"
    assert data == {"text": "结论"}
    with pytest.raises(BackendError):
        parse_sse_block("event: bad\ndata: {oops")


def test_iter_sse_events_yields_complete_frames_only():
    lines = ["event: status", 'data: {"stage": "a"}', "", "event: status",
             'data: {"stage": "b"}', ""]
    assert list(iter_sse_events(lines)) == [
        ("status", {"stage": "a"}), ("status", {"stage": "b"})]


def test_requests_carry_loopback_origin_automatically():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["origin"] = request.headers.get("origin")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"success": True, "data": {"status": "ok"},
                                         "error": None})

    with _client(handler) as client:
        assert client.health() == {"status": "ok"}
    assert seen == {"origin": "http://127.0.0.1:8001", "path": "/health"}


def test_error_envelope_and_unreachable_backend_are_typed():
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "data": None,
                                         "error": {"code": "LOCAL_DEMO_ONLY",
                                                   "message": "本机演示模式禁止远程访问"}})

    with _client(failing) as client, pytest.raises(BackendError) as captured:
        client.health()
    assert captured.value.code == "LOCAL_DEMO_ONLY"
    assert captured.value.status == 403
    assert "本机演示模式" in str(captured.value)

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _client(offline) as client, pytest.raises(BackendError) as captured:
        client.health()
    assert captured.value.code == "BACKEND_UNREACHABLE"
    assert "无法连接 RAG 服务" in str(captured.value)

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with _client(garbage) as client, pytest.raises(BackendError) as captured:
        client.health()
    assert captured.value.code == "INVALID_RESPONSE"


def test_read_endpoints_unwrap_envelopes_and_query_parameters():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path == "/document-jobs":
            return httpx.Response(200, json={"success": True, "data": [
                {"job_id": "j1", "status": "running", "stage": "embed"},
                "ignored",
            ], "error": None})
        if request.url.path == "/conversations/c1":
            return httpx.Response(200, json={"success": True,
                                             "data": {"id": "c1", "title": "标题"},
                                             "error": None})
        return httpx.Response(200, json={"success": True,
                                         "data": [{"id": "c1", "title": "标题"}],
                                         "error": None})

    with _client(handler) as client:
        jobs = client.document_jobs(active=True, limit=5)
        assert jobs == [{"job_id": "j1", "status": "running", "stage": "embed"}]
        assert client.conversations() == [{"id": "c1", "title": "标题"}]
        assert client.conversation("c1") == {"id": "c1", "title": "标题"}
        client.delete_conversation("c1")
    assert seen[0][:2] == ("GET", "/document-jobs")
    assert seen[0][2] == {"active": "true", "limit": "5"}
    assert ("DELETE", "/conversations/c1") in [item[:2] for item in seen]


def test_stream_chat_returns_done_result_and_forwards_events():
    done = {"run_id": "r1", "status": "success",
            "output": {"answer": "答案", "citations": [{"chunk_id": "c1"}]},
            "trace": [{"node_id": "retrieval.dense", "duration_ms": 12.5}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/runs/stream"
        assert request.headers.get("accept") == "text/event-stream"
        body = json.loads(request.content)
        assert body["conversation_id"] == "c1"
        assert body["inputs"]["question"] == "问题"
        return httpx.Response(200, content=_sse(
            ("chat.progress", {"stage": "answer"}),
            ("status", {"stage": "retrieving", "evidence": 3, "web_search": False}),
            ("claim", {"text": "结论", "citations": []}),
            ("done", done),
        ))

    events = []
    with _client(handler) as client:
        result = client.stream_chat("问题", "c1", on_event=lambda name, data: events.append((name, data)))
    assert result == done
    assert [name for name, _ in events] == ["chat.progress", "status", "claim", "done"]


def test_stream_error_frame_raises_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            ("error", {"code": "DEPENDENCY_ERROR", "message": "检索服务不可用", "status": 503})))

    with _client(handler) as client, pytest.raises(BackendError) as captured:
        client.stream_chat("问题", "c1")
    assert captured.value.code == "DEPENDENCY_ERROR"
    assert captured.value.status == 503


def test_stream_without_done_is_reported_and_pre_stream_error_unwraps():
    def no_done(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(("status", {"stage": "retrieving"})))

    with _client(no_done) as client, pytest.raises(BackendError) as captured:
        client.stream_chat("问题", "c1")
    assert captured.value.code == "STREAM_INTERRUPTED"

    def locked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"success": False, "data": None,
                                         "error": {"code": "SECURITY_NOT_READY",
                                                   "message": "安全能力尚未就绪"}})

    with _client(locked) as client, pytest.raises(BackendError) as captured:
        client.stream_chat("问题", "c1")
    assert captured.value.code == "SECURITY_NOT_READY"


def test_timeout_is_reported_as_backend_timeout():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with _client(slow) as client, pytest.raises(BackendError) as captured:
        client.health()
    assert captured.value.code == "BACKEND_TIMEOUT"


def test_chat_once_uses_non_streaming_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/runs"
        return httpx.Response(200, json={"success": True,
                                         "data": {"run_id": "r1", "status": "success",
                                                  "output": {"answer": "答案"}},
                                         "error": None})

    done_events = []
    with _client(handler) as client:
        result = client.chat_once("问题", "c1",
                                  on_event=lambda name, data: done_events.append(name))
    assert result["output"]["answer"] == "答案"
    assert done_events == ["done"]
