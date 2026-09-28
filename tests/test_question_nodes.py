import asyncio
import json

import pytest

from medical_rag.types import ApiError
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.nodes.question import (
    RoutedQuestionNode, decompose, hyde, rewrite, route_question, select, stepback,
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


@pytest.mark.parametrize("route", ["direct", "rewrite", "decompose", "hyde", "stepback"])
def test_router_accepts_only_five_routes(route):
    transport = Transport(json.dumps({"route": route}))
    assert route_question(INPUT, {}, CONTEXT, transport) == {"route": route}
    assert len(transport.calls) == 1


@pytest.mark.parametrize("reply", ["not json", "{}", '{"route":"unknown"}', '{"route":[]}'])
def test_router_invalid_reply_goes_direct(reply):
    assert route_question(INPUT, {}, CONTEXT, Transport(reply)) == {"route": "direct"}


@pytest.mark.parametrize("function,reply,expected", [
    (rewrite, '{"question":"高血压患者的随访频率？"}', ["高血压患者的随访频率？"]),
    (decompose, '{"questions":["频率？","内容？"]}', ["频率？", "内容？"]),
    (stepback, '{"question":"高血压随访原则？"}', ["高血压随访原则？"]),
])
def test_selected_question_transforms(function, reply, expected):
    assert function(INPUT, {}, CONTEXT, Transport(reply))["variant"]["questions"] == expected


def test_hyde_produces_untrusted_vector_text():
    variant = hyde(INPUT, {}, CONTEXT, Transport('{"hypothetical":"随访应记录血压。"}'))["variant"]
    assert variant == {"active": True, "questions": [], "hypothetical": "随访应记录血压。"}


@pytest.mark.parametrize("function,reply", [
    (rewrite, "invalid"), (rewrite, '{"question":""}'),
    (rewrite, json.dumps({"question": "字" * 257})),
    (decompose, json.dumps({"questions": ["a", "b", "c", "d"]})),
    (hyde, json.dumps({"hypothetical": "字" * 601})),
])
def test_invalid_transform_falls_back_to_original(function, reply):
    variant = function(INPUT, {}, CONTEXT, Transport(reply))["variant"]
    assert variant["questions"] == [INPUT["question"]]
    assert variant["hypothetical"] is None


def test_unselected_branch_is_skipped_without_model_call():
    transport = Transport('{"question":"不该调用"}')
    node = RoutedQuestionNode("question.rewrite", "重写", "question",
                              {"question": "string", "provider": "string", "route": "string"},
                              {"variant": "query_variant"},
                              lambda inputs, config, context: rewrite(inputs, config, context, transport))
    result = asyncio.run(node.execute({**INPUT, "route": "direct"}, {}, CONTEXT))
    assert result.status == "skipped"
    assert result.outputs["variant"]["active"] is False
    assert transport.calls == []


def test_transport_failure_is_not_treated_as_invalid_json():
    class BrokenTransport:
        def complete(self, provider, system, user):
            raise ApiError("MODEL_RATE_LIMITED", "稍后重试", 429)

    with pytest.raises(ApiError, match="稍后重试"):
        route_question(INPUT, {}, CONTEXT, BrokenTransport())


@pytest.mark.parametrize("route,expected", [
    ("direct", ["高血压如何随访？"]),
    ("rewrite", ["高血压如何随访？", "随访要求？"]),
    ("decompose", ["高血压如何随访？", "频率？", "内容？"]),
    ("hyde", ["高血压如何随访？"]),
    ("stepback", ["高血压如何随访？", "随访原则？"]),
])
def test_selector_uses_only_chosen_branch_and_original(route, expected):
    inactive = {"active": False, "questions": [], "hypothetical": None}
    inputs = {"question": "高血压如何随访？", "route": route,
              "rewrite": inactive, "decompose": inactive,
              "hyde": inactive, "stepback": inactive}
    selected = {
        "rewrite": {"active": True, "questions": ["随访要求？"], "hypothetical": None},
        "decompose": {"active": True, "questions": ["频率？", "内容？"], "hypothetical": None},
        "hyde": {"active": True, "questions": [], "hypothetical": "规律记录血压。"},
        "stepback": {"active": True, "questions": ["随访原则？"], "hypothetical": None},
    }
    if route != "direct":
        inputs[route] = selected[route]
    plan = select(inputs, {}, CONTEXT)["query_plan"]
    assert plan["questions"] == expected
    assert plan["hypothetical"] == ("规律记录血压。" if route == "hyde" else None)


def test_selector_deduplicates_original_against_hyde_and_other_questions():
    inactive = {"active": False, "questions": [], "hypothetical": None}
    inputs = {"question": "问题", "route": "hyde", "rewrite": inactive,
              "decompose": inactive, "stepback": inactive,
              "hyde": {"active": True, "questions": [], "hypothetical": " 问题 "}}
    assert select(inputs, {}, CONTEXT)["query_plan"] == {
        "questions": ["问题"], "hypothetical": None}


def test_selector_rejects_inactive_chosen_branch():
    with pytest.raises(ValueError, match="所选问题分支未生成结果"):
        select({"question": "问题", "route": "rewrite",
                "rewrite": {"active": False, "questions": [], "hypothetical": None}}, {}, CONTEXT)
