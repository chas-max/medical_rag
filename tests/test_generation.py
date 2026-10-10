import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import (APIConnectionError, APIStatusError, APITimeoutError,
                    AuthenticationError, PermissionDeniedError, RateLimitError)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.config import Settings
from medical_rag.generation import ABSTENTION, GroundedGenerator, OpenAITransport
from medical_rag.types import ApiError, Chunk, Evidence


def test_settings_expose_only_qwen_provider_fields():
    assert {name for name in Settings.model_fields if name.startswith(("qwen_", "deepseek_"))} == {
        "qwen_api_key", "qwen_base_url", "qwen_model"
    }


def evidence():
    return Evidence(Chunk("block1", "doc", "guide.md", "高血压患者应规律随访。", "guide.md", "管理", 2, 0), 0.9)


class Transport:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete(self, provider, system, user):
        self.calls.append((provider, user))
        return self.result


def test_no_evidence_abstains_without_calling_model():
    transport = Transport("invalid")
    result = GroundedGenerator(transport).answer("问题", [], "qwen")
    assert result["abstained"] is True
    assert transport.calls == []


def test_every_claim_receives_a_verified_quote_and_source():
    transport = Transport(json.dumps({"claims": [{"text": "应规律随访", "citations": [{"chunk_id": "block1", "excerpt": "应规律随访"}]}]}))
    result = GroundedGenerator(transport).answer("如何管理？", [evidence()], "qwen")
    assert result["abstained"] is False
    assert "[block1]" in result["answer"]
    assert result["citations"][0]["filename"] == "guide.md"
    assert result["citations"][0]["page"] == 2


@pytest.mark.parametrize("response", [
    "not json",
    '{"claims":[{"text":"未经支持的内容","citations":[]}]}',
    '{"claims":[{"text":"内容","citations":[{"chunk_id":"other","excerpt":"应规律随访"}]}]}',
    '{"claims":[{"text":"内容","citations":[{"chunk_id":"block1","excerpt":"不存在的原文"}]}]}',
])
def test_invalid_citation_or_response_abstains(response):
    result = GroundedGenerator(Transport(response)).answer("问题", [evidence()], "qwen")
    assert result["abstained"] is True
    assert result["citations"] == []


def test_transport_requires_provider_key_before_network():
    transport = OpenAITransport(Settings(qwen_api_key=""))
    with pytest.raises(ApiError, match="API Key"):
        transport.complete("qwen", "system", "user")


def test_transport_uses_only_configured_qwen_model():
    models, options = [], []

    class Completions:
        def create(self, **kwargs):
            models.append(kwargs["model"])
            return type("Response", (), {"choices": [type("Choice", (), {
                "message": type("Message", (), {"content": "{}"})()})()]})()

    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)
            self.chat = type("Chat", (), {"completions": Completions()})()

    transport = OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client)
    assert transport.complete("qwen", "system", "user") == "{}"
    assert models == ["qwen3.8-flash"]
    assert options[0]["max_retries"] == 0
    with pytest.raises(ApiError) as rejected:
        transport.complete("deepseek", "system", "user")
    assert rejected.value.code == "INVALID_PROVIDER"


@pytest.mark.parametrize("error_type,status,code,http_status", [
    (AuthenticationError, 401, "MODEL_AUTH_FAILED", 503),
    (PermissionDeniedError, 403, "MODEL_AUTH_FAILED", 503),
    (RateLimitError, 429, "MODEL_RATE_LIMITED", 429),
    (APIStatusError, 500, "MODEL_UPSTREAM_ERROR", 502),
])
def test_transport_maps_upstream_status_without_exposing_body(error_type, status, code, http_status):
    request = httpx.Request("POST", "https://example.invalid/chat")
    upstream = httpx.Response(status, request=request)

    class Client:
        def __init__(self, **kwargs):
            def fail(**kwargs):
                raise error_type("secret upstream", response=upstream, body={"secret": "private"})
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=fail))

    transport = OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client)
    with pytest.raises(ApiError) as raised:
        transport.complete("qwen", "system", "question")
    assert (raised.value.code, raised.value.status) == (code, http_status)
    assert "secret" not in raised.value.message
    assert "private" not in raised.value.message


@pytest.mark.parametrize("error", [
    APITimeoutError(request=httpx.Request("POST", "https://example.invalid/chat")),
    APIConnectionError(request=httpx.Request("POST", "https://example.invalid/chat")),
])
def test_transport_reports_network_failure_without_raw_error(error):
    class Client:
        def __init__(self, **kwargs):
            def fail(**kwargs):
                raise error
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=fail))

    with pytest.raises(ApiError) as raised:
        OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client).complete("qwen", "s", "u")
    assert (raised.value.code, raised.value.status) == ("MODEL_UNAVAILABLE", 503)


def test_draft_is_not_public_until_separate_citation_validation():
    transport = Transport(json.dumps({"claims": [{"text": "应规律随访", "citations": [
        {"chunk_id": "block1", "excerpt": "规律随访"}]}]}))
    generator = GroundedGenerator(transport)
    # 草稿步骤交回未校验的 draft 文本与本次实际使用的证据，供引用校验节点复核。
    produced = generator.draft("如何管理？", [evidence()], "qwen")
    assert isinstance(produced["draft"], str)
    assert "claims" in produced["draft"]
    assert produced["evidence"] == [evidence()]
    result = generator.validate(produced["draft"], produced["evidence"])
    assert result["abstained"] is False
    assert result["citations"][0]["chunk_id"] == "block1"


def test_generation_prompt_limits_claim_count_to_validator_limit():
    class PromptTransport:
        def __init__(self):
            self.system = ""

        def complete(self, provider, system, user):
            self.system = system
            return '{"claims":[]}'

    transport = PromptTransport()
    GroundedGenerator(transport).draft("问题", [evidence()], "qwen")

    assert "最多 8 条" in transport.system
    assert "逐字复制" in transport.system
    assert "优先 3–5 条" in transport.system
    assert "第 1 条直接回答问题" in transport.system
    # 摘录越长越容易在改写或跨行时对不上原文，整条被逐字校验丢掉。
    assert "短句" in transport.system


def test_prompt_separates_grounded_claims_from_the_free_analysis_section():
    """分析段故意允许发散，提示词必须说清它的边界：可复述已引结论，不得新增医学事实。"""
    class PromptTransport:
        def __init__(self):
            self.system = ""

        def complete(self, provider, system, user):
            self.system = system
            return '{"claims":[]}'

    transport = PromptTransport()
    GroundedGenerator(transport).draft("问题", [evidence()], "qwen")

    assert "analysis" in transport.system
    assert "based_on" in transport.system
    # 允许发散的同时，仍然禁止把无出处的医学事实写进解释段。
    assert "严禁新增任何医学事实" in transport.system
    assert "不得引入证据之外的医学事实" in transport.system
    # 复述已验证结论里的数值是允许的，反馈里明确禁止会出现"安全的复述被删、新断言放行"。
    assert "复述" in transport.system
    # 给出填好的正例，比单纯的形容词更能约束输出形状。
    assert '"based_on":["c1"]' in transport.system
    # analysis 可以省略，避免模型为了满足字段形状硬凑空话。
    assert "省略 analysis" in transport.system


def test_validator_caps_excess_claims_after_validating_the_visible_prefix():
    claims = [
        {"text": f"结论 {index}", "citations": [{"chunk_id": "block1", "excerpt": "规律随访"}]}
        for index in range(9)
    ]

    result = GroundedGenerator(Transport(json.dumps({"claims": claims}))).answer(
        "问题", [evidence()], "qwen"
    )

    assert result["abstained"] is False
    assert result["answer"].count("[block1]") == 8


def test_validator_accepts_model_excerpt_with_only_whitespace_normalized():
    item = Evidence(
        Chunk("block1", "doc", "guide.md", "血压\n控制满意。", "guide.md", "管理", 2, 0),
        0.9,
    )
    output = json.dumps({"claims": [{
        "text": "血压应控制满意",
        "citations": [{"chunk_id": "block1", "excerpt": "血压 控制满意。"}],
    }]})

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [item], "qwen")

    assert result["abstained"] is False
    assert result["citations"][0]["excerpt"] == "血压\n控制满意。"


def test_validator_accepts_whitespace_inserted_or_removed_inside_excerpt():
    item = Evidence(
        Chunk("block1", "doc", "guide.md", "2 周内随访。", "guide.md", "管理", 2, 0),
        0.9,
    )
    output = json.dumps({"claims": [{
        "text": "应在两周内随访",
        "citations": [{"chunk_id": "block1", "excerpt": "2周内随访。"}],
    }]})

    result = GroundedGenerator(Transport(output)).answer("何时随访？", [item], "qwen")

    assert result["abstained"] is False
    assert result["citations"][0]["excerpt"] == "2 周内随访。"


def test_validator_keeps_verified_claims_when_another_claim_has_bad_citation():
    output = json.dumps({"claims": [
        {"text": "应规律随访", "citations": [{"chunk_id": "block1", "excerpt": "应规律随访"}]},
        {"text": "未经支持的结论", "citations": [{"chunk_id": "block1", "excerpt": "不存在的原文"}]},
    ]})

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["answer"].startswith("应规律随访 [block1]")
    assert "未经支持的结论" not in result["answer"]


def test_validator_abstains_when_all_claims_have_bad_citations():
    output = json.dumps({"claims": [
        {"text": "未经支持的结论", "citations": [{"chunk_id": "block1", "excerpt": "不存在的原文"}]},
    ]})

    result = GroundedGenerator(Transport(output)).answer("问题", [evidence()], "qwen")

    assert result["abstained"] is True
    assert result["citations"] == []


def test_answer_reports_model_unavailability_without_returning_retrieved_excerpt():
    class UnavailableTransport:
        def complete(self, provider, system, user):
            raise TimeoutError("模型服务不可达")

    with pytest.raises(ApiError) as raised:
        GroundedGenerator(UnavailableTransport()).answer("如何管理？", [evidence()], "qwen")

    assert raised.value.code == "MODEL_UNAVAILABLE"
    assert raised.value.status == 503
    assert evidence().chunk.text not in str(raised.value)


def test_answer_does_not_hide_non_connection_model_errors():
    class InvalidModelTransport:
        def complete(self, provider, system, user):
            raise ValueError("模型配置错误")

    with pytest.raises(ValueError, match="模型配置错误"):
        GroundedGenerator(InvalidModelTransport()).answer("如何管理？", [evidence()], "qwen")


def analysis_document(summary="规律随访是管理慢性病的基础。", extensions=None, based_on=None,
                      text="应规律随访", excerpt="规律随访"):
    return json.dumps({"claims": [{"text": text, "citations": [
        {"chunk_id": "block1", "excerpt": excerpt}]}],
        "analysis": {"summary": summary, "extensions": extensions or [],
                     "based_on": based_on or ["block1"]}}, ensure_ascii=False)


def test_analysis_section_expands_the_answer_without_entering_the_verified_answer():
    output = analysis_document(extensions=["定期随访便于医生及时发现病情变化。"])

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    # answer 仍然只含逐字校验过的结论，评测与旧调用方拿到的一模一样。
    assert result["answer"] == "应规律随访 [block1]"
    assert result["analysis"]["summary"] == "规律随访是管理慢性病的基础。"
    assert result["analysis"]["extensions"] == ["定期随访便于医生及时发现病情变化。"]
    assert result["analysis"]["based_on"] == ["block1"]


def test_analysis_is_absent_when_the_draft_carries_none():
    output = json.dumps({"claims": [{"text": "应规律随访", "citations": [
        {"chunk_id": "block1", "excerpt": "规律随访"}]}]})

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert "analysis" not in result


def test_analysis_never_becomes_a_channel_around_verbatim_validation():
    """结论全部被逐字校验丢掉时，附带的解释段必须一起丢掉，否则等于绕过引用校验。"""
    output = json.dumps({"claims": [{"text": "伪造结论", "citations": [
        {"chunk_id": "block1", "excerpt": "不存在的原文"}]}],
        "analysis": {"summary": "这段解释本身读起来很合理。", "extensions": [],
                     "based_on": ["block1"]}}, ensure_ascii=False)

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result == {"answer": ABSTENTION, "citations": [], "abstained": True}


@pytest.mark.parametrize("analysis", [
    # 无所依据：analysis 只能挂在已验证结论引用过的块上。
    {"summary": "规律随访很重要。", "extensions": [], "based_on": ["other"]},
    {"summary": "规律随访很重要。", "extensions": [], "based_on": []},
    # 形状不对的一律丢弃，不做修补。
    {"summary": "", "extensions": [], "based_on": ["block1"]},
    {"summary": "   ", "extensions": [], "based_on": ["block1"]},
    {"summary": 123, "extensions": [], "based_on": ["block1"]},
    {"summary": "规律随访很重要。", "extensions": [], "based_on": [1, None]},
    # 不可迭代的 based_on / extensions 必须只丢掉分析段，不能抛异常连累已验证结论。
    {"summary": "规律随访很重要。", "based_on": 5},
    {"summary": "规律随访很重要。", "based_on": True},
    {"summary": "规律随访很重要。", "based_on": None},
    {"summary": "规律随访很重要。", "based_on": "block1"},
    {"summary": "规律随访很重要。", "based_on": ["block1"], "extensions": 5},
    # 字符串 extensions 会被逐字符拆开渲染，必须整段丢弃而不是"修成"单个汉字。
    {"summary": "规律随访很重要。", "based_on": ["block1"], "extensions": "建议尽快就医"},
    "规律随访很重要。",
    ["规律随访很重要。"],
    5,
    None,
])
def test_invalid_analysis_is_dropped_while_claims_survive(analysis):
    """分析段再离谱也只影响它自己：answer/citations/abstained 必须保持已校验结论的语义。"""
    output = json.dumps({"claims": [{"text": "应规律随访", "citations": [
        {"chunk_id": "block1", "excerpt": "规律随访"}]}], "analysis": analysis}, ensure_ascii=False)

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["answer"] == "应规律随访 [block1]"
    assert "analysis" not in result


@pytest.mark.parametrize("summary", [
    # 复述已验证结论里的数值是安全的，必须放行；按字面禁数字会把这段整段删掉。
    "血压目标与指南给出的数值一致，需要长期坚持。",
    "通常需要按固定间隔复查一次。",
    "复查的松紧取决于血压是否达标以及正在用什么方案。",
    "这是长期管理的一部分，也是十分常见的做法。",
])
def test_analysis_accepts_safe_restatements_of_verified_values(summary):
    result = GroundedGenerator(Transport(analysis_document(summary=summary))).answer(
        "如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["analysis"]["summary"] == summary


def test_analysis_only_keeps_blocks_that_a_verified_claim_already_cited():
    """based_on 是唯一的结构性锚点：没被已验证结论引用过的块静默剔除，而不是放行。"""
    output = json.dumps({
        "claims": [{"text": "应规律随访", "citations": [
            {"chunk_id": "block1", "excerpt": "规律随访"}]}],
        "analysis": {"summary": "把结论串起来解释一下。", "extensions": [],
                     "based_on": ["block1", "never-cited", "other"]},
    }, ensure_ascii=False)

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result["analysis"]["based_on"] == ["block1"]


def test_analysis_keeps_only_valid_extensions_up_to_the_cap():
    output = analysis_document(extensions=[
        "随访便于医生掌握病情变化。", "", "记录用药后的感受。",
        "复诊时带上既往检查资料。", "出现不适及时就医。"])

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    # 空串被丢，其余按原顺序保留；上限按"通过校验的条数"计算，不因丢条而少给。
    assert result["analysis"]["extensions"] == [
        "随访便于医生掌握病情变化。", "记录用药后的感受。", "复诊时带上既往检查资料。"]
