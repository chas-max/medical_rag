import json

import pytest

from medical_rag.types import ApiError
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.nodes.merge import collect
from medical_rag.workflow.nodes.question import (
    decompose, hyde, rewrite, route_question, stepback,
)


class Transport:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def complete(self, provider, system, user):
        self.calls.append((provider, system, json.loads(user)))
        return self.reply


CONTEXT = ExecutionContext("run", "medical-answer-expanded", {})
INPUT = {"question": "高血压如何随访？", "provider": "qwen"}
DEFAULT = "qwen"


@pytest.mark.parametrize("route", ["direct", "rewrite", "decompose", "hyde", "stepback"])
def test_router_accepts_only_five_routes(route):
    transport = Transport(json.dumps({"route": route}))
    assert route_question(INPUT, {}, CONTEXT, transport, DEFAULT) == {"route": route}
    assert len(transport.calls) == 1


@pytest.mark.parametrize("reply", ["not json", "{}", '{"route":"unknown"}', '{"route":[]}'])
def test_router_invalid_reply_goes_direct(reply):
    value = route_question(INPUT, {}, CONTEXT, Transport(reply), DEFAULT)
    assert value == {"route": "direct"}


@pytest.mark.parametrize("function,reply,expected", [
    (rewrite, '{"question":"高血压患者的随访频率？"}', "高血压患者的随访频率？"),
    (stepback, '{"question":"高血压随访原则？"}', "高血压随访原则？"),
])
def test_single_question_transforms_output_plain_text(function, reply, expected):
    """改写与上位问题直接产出可检索文本，不再包一层私有结构。"""
    assert function(INPUT, {}, CONTEXT, Transport(reply), DEFAULT)["query"] == expected


def test_decompose_output_is_a_list_of_questions():
    value = decompose(INPUT, {}, CONTEXT, Transport('{"questions":["频率？","内容？"]}'), DEFAULT)
    assert value == {"queries": ["频率？", "内容？"]}


def test_hyde_output_is_vector_text_only():
    value = hyde(INPUT, {}, CONTEXT, Transport('{"hypothetical":"随访应记录血压。"}'), DEFAULT)
    assert value == {"query": "随访应记录血压。"}


@pytest.mark.parametrize("function,reply", [
    (rewrite, "invalid"),
    (rewrite, '{"question":""}'),
    (rewrite, json.dumps({"question": "字" * 257})),
    (stepback, "not json"),
    (stepback, '{"question":"   "}'),
    (decompose, json.dumps({"questions": ["a", "b", "c", "d"]})),
    (hyde, json.dumps({"hypothetical": "字" * 601})),
    (hyde, "{}"),
])
def test_invalid_transform_falls_back_to_original(function, reply):
    produced = function(INPUT, {}, CONTEXT, Transport(reply), DEFAULT)
    if function is decompose:
        assert produced["queries"] == [INPUT["question"]]
    else:
        assert produced["query"] == INPUT["question"]


def test_transforms_run_without_a_provider_wire():
    """provider 端口是可选的：不连线时用运行默认值，节点因此不依赖输入节点也能跑。"""
    transport = Transport('{"question":"随访频率？"}')
    rewrite({"question": "高血压如何随访？"}, {}, CONTEXT, transport, "qwen")
    assert transport.calls[0][0] == "qwen"


def test_transport_failure_is_not_treated_as_invalid_json():
    class BrokenTransport:
        def complete(self, provider, system, user):
            raise ApiError("MODEL_RATE_LIMITED", "稍后重试", 429)

    with pytest.raises(ApiError, match="稍后重试"):
        route_question(INPUT, {}, CONTEXT, BrokenTransport(), DEFAULT)


def test_collect_merges_every_connected_producer_in_order():
    value = collect({"items": ["原问题", "随访频率？", "原问题", " 上位问题 "]}, {}, CONTEXT)
    assert value == {"values": ["原问题", "随访频率？", "上位问题"]}


def test_collect_tolerates_branches_that_produced_nothing():
    assert collect({}, {}, CONTEXT) == {"values": []}
