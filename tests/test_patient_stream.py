"""Patient streaming: frame hygiene, and persistence only for a complete answer.

No database and no model: these are the pure pieces the PostgreSQL suite builds on.
"""

import json
from uuid import uuid4

import httpx
import pytest

from medical_rag.dialogue_models import DialogueSnapshot
from medical_rag.identity_sessions import SessionStore
from medical_rag.patient_chat import ChatClient, ChatUnavailable
from medical_rag.patient_service import HealthNotice, PatientDenied, PatientService, StreamPlan

BASE = "http://127.0.0.1:8001"
SECRET = "fictional-upstream-detail-NEVER-RETURN"
DONE = {"answer": "结论一", "abstained": False, "answer_type": "answer", "citations": []}


def sse(*frames):
    return "".join(
        f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for name, data in frames)


def client(handler):
    return ChatClient(BASE, 5, 4, transport=httpx.MockTransport(handler))


def stream_handler(body, *, status=200, content_type="text/event-stream"):
    def handler(request):
        return httpx.Response(status, headers={"content-type": content_type},
                              content=body.encode(), request=request)
    return handler


def test_stream_relays_claims_then_the_validated_answer():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["accept"] = request.headers["accept"]
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(
                                  ("status", {"stage": "generating", "evidence": 3}),
                                  ("claim", {"text": "结论一", "citations": [{"chunk_id": "c1"}]}),
                                  ("analysis", {"summary": "分析"}),
                                  ("done", {**DONE, "citations": [
                                      {"chunk_id": "c1", "filename": "指南.pdf",
                                       "section": "第1章", "page": 3}]}),
                              ).encode(), request=request)

    frames = list(client(handler).stream("问题"))

    assert seen["path"] == "/answers/stream"
    assert seen["accept"] == "text/event-stream"
    assert seen["body"]["memory"] is False and seen["body"]["top_k"] == 4
    assert [frame["event"] for frame in frames] == ["status", "claim", "analysis", "done"]
    # The claim keeps only the text: citations are authoritative only in ``done``.
    assert frames[1]["data"] == {"text": "结论一"}
    assert frames[-1]["data"]["citations"] == [
        {"chunk_id": "c1", "filename": "指南.pdf", "section": "第1章", "page": 3}]


def test_unknown_event_names_do_not_break_an_installed_portal():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(("future.event", {"x": 1}),
                                          ("claim", {"text": "结论"}),
                                          ("done", DONE)).encode(), request=request)

    frames = list(client(handler).stream("问题"))

    assert [frame["event"] for frame in frames] == ["claim", "done"]


def test_upstream_error_frame_is_static_and_its_text_never_propagates():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(("error", {"code": SECRET, "message": SECRET})).encode(),
                              request=request)

    with pytest.raises(ChatUnavailable) as caught:
        list(client(handler).stream("问题"))

    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("body", [
    sse(("status", {"stage": "generating"})),          # ended without a terminal frame
    "",                                                 # empty body
])
def test_a_stream_without_done_is_a_failed_turn(body):
    with pytest.raises(ChatUnavailable):
        list(client(stream_handler(body)).stream("问题"))


@pytest.mark.parametrize(("status", "content_type"), [
    (503, "text/event-stream"),
    (200, "application/json"),
])
def test_non_streaming_upstream_is_unavailable(status, content_type):
    with pytest.raises(ChatUnavailable):
        list(client(stream_handler(sse(("done", DONE)), status=status,
                                   content_type=content_type)).stream("问题"))


def test_an_oversized_claim_text_is_dropped_not_shown():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(("claim", {"text": "x" * 2001}),
                                          ("done", DONE)).encode(), request=request)

    frames = list(client(handler).stream("问题"))

    assert [frame["event"] for frame in frames] == ["done"]


def test_an_oversized_frame_is_a_protocol_failure():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(("status", {"stage": "x" * 20000})).encode(),
                              request=request)

    with pytest.raises(ChatUnavailable):
        list(client(handler).stream("问题"))


class FakeStream:
    """Answer source that only implements the streaming half."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = []

    def answer(self, question, **kwargs):
        raise AssertionError("the streaming path must not fall back to answer()")

    def stream(self, question, **kwargs):
        self.calls.append((question, kwargs))
        yield from self.frames


def relay(frames, finish=None):
    """Build the relay without a database: only the frame logic is under test."""
    service = object.__new__(PatientService)
    service.chat = FakeStream(frames)
    service.dialogue_state_enabled = False
    service.chat_streaming = True
    persisted = []

    def default_finish(token, *, csrf_token, plan, answer):
        persisted.append(answer)
        return {"conversation": {"id": "c1"}, "answer": {"content": "结论一"}}

    service.finish_stream = finish or default_finish
    plan = StreamPlan(None, DialogueSnapshot(), "问题")
    return service, plan, persisted


def test_relay_drops_analysis_and_persists_only_after_done():
    service, plan, persisted = relay([
        {"event": "status", "data": {"stage": "generating"}},
        {"event": "claim", "data": {"text": "结论一"}},
        {"event": "analysis", "data": {"summary": "分析段"}},
        {"event": "done", "data": DONE},
    ])

    frames = list(service._frames("token", csrf_token="csrf", plan=plan))

    assert [frame["event"] for frame in frames] == ["status", "status", "claim", "done"]
    assert frames[0]["data"] == {"stage": "accepted"}
    assert frames[-1]["data"] == {"conversation": {"id": "c1"}, "answer": {"content": "结论一"}}
    assert persisted == [DONE]


def test_relay_never_persists_a_stream_that_ends_early():
    service, plan, persisted = relay([
        {"event": "claim", "data": {"text": "结论一"}},
    ])

    with pytest.raises(ChatUnavailable):
        list(service._frames("token", csrf_token="csrf", plan=plan))

    assert persisted == []


def test_relay_never_persists_after_a_failed_upstream():
    service, plan, persisted = relay([
        {"event": "claim", "data": {"text": "结论一"}},
        {"event": "malformed"},
    ])

    with pytest.raises(ChatUnavailable):
        list(service._frames("token", csrf_token="csrf", plan=plan))

    assert persisted == []


def test_open_stream_authorizes_before_any_stream_can_start():
    service = object.__new__(PatientService)
    service.chat = object()

    def deny(*args, **kwargs):
        raise PatientDenied()

    service.prepare_stream = deny

    # A generator function would defer this to the first iteration, by which time
    # the HTTP layer has already committed to a 200 stream.
    with pytest.raises(PatientDenied):
        service.open_stream("token", csrf_token="csrf", conversation_id=None, question="问题")


def test_streaming_configuration_requires_a_streaming_answerer():
    sessions = SessionStore(lambda: None, tenant_id=str(uuid4()))
    notice = HealthNotice("fictional-v1", "Fictional development notice.", 3600)

    class AnswerOnly:
        def answer(self, question, **kwargs):
            return dict(DONE)

    with pytest.raises(ValueError):
        PatientService(sessions, notice, AnswerOnly(), chat_streaming=True)

    service = PatientService(sessions, notice, AnswerOnly())
    assert service.chat_streaming is False
