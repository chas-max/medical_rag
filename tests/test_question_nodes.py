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



def test_hybrid_plan_routes_lightweight_decompose_and_hyde():
    from medical_rag.workflow.nodes.question import hybrid_plan

    numbered = hybrid_plan(
        {"question": "一位72岁男性高血压合并糖尿病患者。（1）降压目标是多少？（2）优选哪些降压药？"},
        {},
        CONTEXT,
    )
    assert numbered["decompose_route"] == "direct"
    assert numbered["hyde_route"] == "skip"
    assert len(numbered["queries"]) == 3

    guidelines = hybrid_plan(
        {"question": "比较《中国高血压防治指南（2024）》与《国家基层高血压防治管理指南（2025）》在观察时间上的异同？"},
        {},
        CONTEXT,
    )
    assert guidelines["decompose_route"] == "direct"
    assert guidelines["hyde_route"] == "skip"
    assert len(guidelines["queries"]) == 3

    implicit_compound = hybrid_plan(
        {"question": "针对妊娠合并甲状腺功能减退的初诊患者，临床常见的早期筛查指标有哪些以及孕期左甲状腺素钠调整原则是什么？"},
        {},
        CONTEXT,
    )
    assert implicit_compound["decompose_route"] == "decompose"
    assert implicit_compound["hyde_route"] == "hyde"

    vague = hybrid_plan(
        {"question": "我最近经常晨起头晕乏力伴有轻微恶心，请问可能是什么原因？"},
        {},
        CONTEXT,
    )
    assert vague["decompose_route"] == "direct"
    assert vague["hyde_route"] == "hyde"


def test_resolve_question_standalone_fallback_keeps_ambiguous_halted():
    from medical_rag.dialogue_models import DialogueRun, DialogueSnapshot
    from medical_rag.question_resolver import QuestionResolver
    from medical_rag.workflow.contracts import Halt
    from medical_rag.workflow.nodes.question import resolve_question

    class TimeoutTransport:
        def complete_with_timeout(self, provider, system, user, timeout):
            raise TimeoutError("timeout")

    resolver = QuestionResolver(TimeoutTransport())
    question = "针对成人初诊高血压合并慢性肾脏病G3a期患者，诊室血压控制目标与首选降压药是什么？"

    halted_default = resolve_question(
        {"question": question}, {}, ExecutionContext("r1", "medical-answer", {}), resolver
    )
    assert isinstance(halted_default, Halt)

    ctx_v8 = ExecutionContext("r2", "medical-answer", {}, dialogue=DialogueRun(DialogueSnapshot()))
    passed = resolve_question(
        {"question": question},
        {"standalone_fallback": True, "preserve_standalone_query": True},
        ctx_v8,
        resolver,
    )
    assert passed == {"query": question}
    assert ctx_v8.dialogue.resolution.status == "resolved"
    assert ctx_v8.dialogue.resolution.relation == "standalone"

    for idx, ambiguous in enumerate(("它是什么引起的？", "CT和MRI哪个好？", "高血压和糖尿病，它是什么引起的？")):
        ctx_amb = ExecutionContext(
            f"amb-{idx}", "medical-answer", {}, dialogue=DialogueRun(DialogueSnapshot())
        )
        res = resolve_question(
            {"question": ambiguous},
            {"standalone_fallback": True},
            ctx_amb,
            resolver,
        )
        assert isinstance(res, Halt)


def test_decompose_fallback_on_error_only_when_enabled():
    class BrokenTransport:
        def complete(self, provider, system, user):
            raise ApiError("MODEL_RATE_LIMITED", "限流", 429)

    with pytest.raises(ApiError):
        decompose({"question": "问题一"}, {}, CONTEXT, BrokenTransport(), DEFAULT)

    fallback = decompose(
        {"question": "问题一"}, {"fallback_on_error": True}, CONTEXT, BrokenTransport(), DEFAULT
    )
    assert fallback == {"queries": ["问题一"]}


def test_hybrid_plan_strip_wrappers_and_expand_synonyms():
    from medical_rag.workflow.nodes.question import hybrid_plan

    ctx = ExecutionContext("run-hp", "medical-answer", {})
    question = (
        "现有医学资料能否提供具体方案？"
        "患者存在肾性骨病合并CKD4期，请根据指南结合临床实际回答："
        "（1）活性维生素D怎么用？（2）低血钙如何处理？请分点说明："
    )
    result = hybrid_plan(
        {"question": question},
        {
            "max_subqueries": 3,
            "strip_wrappers": True,
            "expand_synonyms": True,
            "max_synonym_variants": 2,
        },
        ctx,
    )
    assert len(result["subqueries"]) == 2
    assert "现有医学资料能否" not in result["queries"][0]
    assert "结合临床实际" not in result["queries"][0]
    assert any("慢性肾脏病" in q for q in result["queries"])

    raw_only = hybrid_plan(
        {"question": "根据指南回答，CKD患者的降压目标是什么？"},
        {"strip_wrappers": False, "expand_synonyms": False},
        ExecutionContext("run-raw", "medical-answer", {}),
    )
    assert raw_only["queries"] == ["根据指南回答，CKD患者的降压目标是什么？"]
    assert raw_only["subqueries"] == []


def test_hybrid_plan_routes_secondary_anaphora_to_decompose():
    from medical_rag.workflow.nodes.question import hybrid_plan

    question = (
        "高血压和2型糖尿病患者常见哪些慢性肾脏病风险？"
        "针对这两类慢病，各自首选的一线干预策略是什么？"
    )
    routed = hybrid_plan(
        {"question": question},
        {"enable_llm_decompose": True, "strip_wrappers": False, "expand_synonyms": False},
        ExecutionContext("run-anaphora", "medical-answer", {}),
    )
    assert routed["decompose_route"] == "decompose"
    assert routed["subqueries"] == []
    assert routed["queries"] == [question]

    disabled = hybrid_plan(
        {"question": question},
        {"enable_llm_decompose": False, "strip_wrappers": False, "expand_synonyms": False},
        ExecutionContext("run-anaphora-off", "medical-answer", {}),
    )
    assert disabled["decompose_route"] == "direct"
    assert len(disabled["subqueries"]) == 2
