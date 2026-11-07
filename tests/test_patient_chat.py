"""Pure tests for the patient-side answer client; no database or model."""

import json

import httpx
import pytest

from medical_rag.patient_chat import ChatClient, ChatUnavailable

BASE = "http://127.0.0.1:8001"


def client(handler, **kwargs):
    return ChatClient(BASE, 5, 4, transport=httpx.MockTransport(handler), **kwargs)


def response(data, status=200, request=None):
    return httpx.Response(status, json={"success": True, "data": data, "error": None},
                          request=request or httpx.Request("POST", BASE + "/answers"))


@pytest.mark.parametrize("value", [
    "https://example.com", "http://10.0.0.1:8001", "http://localhost:8001/path",
    "http://user:pass@127.0.0.1:8001", "http://127.0.0.1:8001?x=1", "not-a-url", "",
])
def test_chat_endpoint_must_be_a_bare_loopback_origin(value):
    with pytest.raises(ValueError):
        ChatClient(value, 5, 4)


@pytest.mark.parametrize("timeout", [0, 301, True, 5.0])
def test_chat_timeout_is_bounded_integer(timeout):
    with pytest.raises(ValueError):
        ChatClient(BASE, timeout, 4)


@pytest.mark.parametrize("top_k", [0, 21, True])
def test_chat_top_k_is_bounded_integer(top_k):
    with pytest.raises(ValueError):
        ChatClient(BASE, 5, top_k)


def test_answer_sends_loopback_origin_and_sanitizes_citations():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["origin"] = request.headers["origin"]
        seen["body"] = request.read().decode()
        return response({
            "answer": "结论 [c1]",
            "abstained": False,
            "citations": [
                {"chunk_id": "c1", "filename": "指南.pdf", "section": "第1章", "page": 3,
                 "origin": "knowledge_base", "excerpt": "原文", "unexpected": "drop"},
                {"chunk_id": "", "filename": "x.pdf"},
                {"chunk_id": "c2", "filename": "x.pdf", "section": 7, "page": "3"},
                {"chunk_id": "c3", "filename": "y.pdf", "page": -1},
                {"chunk_id": "c4", "filename": "z.pdf", "excerpt": "x" * 5000},
            ],
        })

    result = client(handler).answer("问题")
    assert seen["path"] == "/answers"
    assert seen["origin"] == BASE
    assert '"top_k":4' in seen["body"].replace(" ", "")
    assert '"memory":false' in seen["body"].replace(" ", "")
    assert result["answer"] == "结论 [c1]"
    assert result["abstained"] is False
    assert [item["chunk_id"] for item in result["citations"]] == ["c1", "c2", "c3", "c4"]
    assert "unexpected" not in result["citations"][0]
    assert result["citations"][0]["page"] == 3
    assert result["citations"][0]["excerpt"] == "原文"
    assert "page" not in result["citations"][1]
    assert result["citations"][1]["section"] == ""
    assert "page" not in result["citations"][2]
    assert "excerpt" not in result["citations"][3]


@pytest.mark.parametrize("payload", [
    {"answer": "", "abstained": False, "citations": []},
    {"answer": "x", "abstained": "no", "citations": []},
    {"answer": "x", "abstained": False, "citations": {}},
    {"answer": "x" * 20001, "abstained": False, "citations": []},
    {"answer": "x", "abstained": False, "citations": [{}] * 51},
])
def test_answer_rejects_malformed_payloads(payload):
    with pytest.raises(ChatUnavailable):
        client(lambda request: response(payload)).answer("q")


@pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
def test_answer_is_static_on_any_non_200(status):
    with pytest.raises(ChatUnavailable):
        client(lambda request: httpx.Response(status, text="upstream-private",
                                              request=httpx.Request("POST", BASE + "/answers"))
               ).answer("q")


@pytest.mark.parametrize("payload", [
    {"success": False, "data": None, "error": {"code": "X", "message": "private"}},
    {"success": True, "data": None, "error": None},
    {"success": True, "data": {"answer": "x", "abstained": False}, "error": "x"},
])
def test_answer_is_static_on_bad_envelope(payload):
    def handler(request):
        return httpx.Response(200, json=payload, request=request)

    with pytest.raises(ChatUnavailable):
        client(handler).answer("q")


def test_transport_failure_and_invalid_question_are_static():
    def handler(request):
        raise httpx.ConnectError("upstream-private", request=request)

    with pytest.raises(ChatUnavailable):
        client(handler).answer("q")
    with pytest.raises(ValueError):
        client(lambda request: response({"answer": "x", "abstained": False})).answer("x" * 16001)
    with pytest.raises(ValueError):
        client(lambda request: response({"answer": "x", "abstained": False})).answer("   ")

# -- bounded dialogue context -----------------------------------------------

from medical_rag.dialogue_models import DialogueSnapshot, RecentTurn


def _resolution(question="那它是由什么引起的？", source="1", topic="糖尿病",
                full="糖尿病是由什么引起的？", original=None):
    return {
        "original_question": original or question, "status": "resolved",
        "relation": "follow_up", "resolved_question": full,
        "topic": {"name": topic, "type": "disease", "source_turn_id": source,
                  "source_role": "user"},
        "intent": "etiology", "intent_text": "由什么引起", "references": [],
        "clarification": None,
    }


def test_patient_client_forwards_only_bounded_context():
    requests = []

    def handler(request):
        requests.append(json.loads(request.read()))
        return response({"answer": "请补充讨论对象。", "citations": [], "abstained": False,
                         "answer_type": "clarification"})

    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答"),))
    result = client(handler).answer("那它是由什么引起的？", dialogue=snapshot)

    assert requests[0]["memory"] is False
    assert requests[0]["dialogue_context"]["recent"][0]["turn_id"] == "1"
    assert "patient_id" not in requests[0]
    assert result["answer_type"] == "clarification"


def test_patient_client_without_context_sends_no_context_key():
    requests = []

    def handler(request):
        requests.append(json.loads(request.read()))
        return response({"answer": "结论", "citations": [], "abstained": False})

    result = client(handler).answer("高血压如何随访？")

    assert "dialogue_context" not in requests[0]
    assert result["answer_type"] == "answer"


def test_patient_client_verifies_the_returned_resolution_against_its_snapshot():
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答"),))

    def handler(request):
        return response({"answer": "合成回答", "citations": [], "abstained": False,
                         "answer_type": "answer",
                         "dialogue_resolution": _resolution()})

    result = client(handler).answer("那它是由什么引起的？", dialogue=snapshot)

    assert result["dialogue_resolution"]["topic"]["name"] == "糖尿病"


@pytest.mark.parametrize("resolution", [
    _resolution(original="别的问题"),
    _resolution(source="999"),
    _resolution(topic="高血压"),
    {"status": "resolved", "relation": "follow_up", "resolved_question": None},
    {"unexpected": True},
])
def test_patient_client_rejects_unverifiable_resolutions(resolution):
    def handler(request):
        return response({"answer": "合成回答", "citations": [], "abstained": False,
                         "dialogue_resolution": resolution})

    with pytest.raises(ChatUnavailable):
        client(handler).answer("那它是由什么引起的？", dialogue=DialogueSnapshot())


def test_patient_client_accepts_an_answer_without_a_resolution():
    def handler(request):
        return response({"answer": "合成回答", "citations": [], "abstained": False})

    result = client(handler).answer("那它是由什么引起的？", dialogue=DialogueSnapshot())

    assert "dialogue_resolution" not in result


def test_patient_client_rejects_an_unknown_answer_type():
    def handler(request):
        return response({"answer": "合成回答", "citations": [], "abstained": False,
                         "answer_type": "medical_advice"})

    with pytest.raises(ChatUnavailable):
        client(handler).answer("问题")
