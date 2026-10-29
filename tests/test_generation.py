import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.config import Settings
from medical_rag.generation import (
    ABSTAIN_PARSE_ERROR,
    ABSTAIN_TRUNCATED,
    ABSTAIN_UNANCHORED,
    ABSTENTION,
    MAX_OUTPUT_TOKENS,
    TRUNCATION_RETRY_MAX_TOKENS,
    Completion,
    GroundedGenerator,
    OpenAITransport,
    completion_options,
    plan_section,
    verify_connection,
)
from medical_rag.types import ApiError, Chunk, Evidence


def test_settings_expose_only_qwen_provider_fields():
    assert {name for name in Settings.model_fields if name.startswith(("qwen_", "deepseek_"))} == {
        "qwen_api_key", "qwen_base_url", "qwen_model"
    }


def test_chunk_settings_stay_within_the_embedding_window():
    settings = Settings()
    assert 100 <= settings.chunk_max_tokens <= 480
    assert 0 <= settings.chunk_overlap_tokens < settings.chunk_max_tokens
    assert settings.parent_max_tokens >= settings.chunk_max_tokens
    with pytest.raises(ValueError):
        Settings(chunk_max_tokens=480, chunk_overlap_tokens=480)
    with pytest.raises(ValueError):
        Settings(chunk_max_tokens=200, parent_max_tokens=100)


def evidence():
    return Evidence(Chunk("block1", "doc", "guide.md", "高血压患者应规律随访。", "guide.md", "管理", 2, 0), 0.9)


def parented_evidence():
    parent = "父块上下文：子块一讲随访，子块二讲测量。"
    first = Chunk("c1", "doc", "guide.md", "子块一讲随访。", "guide.md", "管理", 2, 0,
                  parent_id="p1", parent_text=parent, parent_ordinal=0, token_count=7)
    second = Chunk("c2", "doc", "guide.md", "子块二讲测量。", "guide.md", "管理", 2, 1,
                   parent_id="p1", parent_text=parent, parent_ordinal=0, token_count=7)
    return [Evidence(first, 0.9), Evidence(second, 0.8)]


def test_generation_sends_one_parent_context_for_sibling_children():
    user = GroundedGenerator(Transport(""))._user("如何随访？", parented_evidence())
    payload = json.loads(user)
    assert len(payload["evidence"]) == 1
    entry = payload["evidence"][0]
    assert entry["chunk_id"] == "c1"
    assert entry["chunk_ids"] == ["c1", "c2"]
    assert entry["text"] == "父块上下文：子块一讲随访，子块二讲测量。"


def test_generation_falls_back_to_child_text_without_a_parent():
    entry = json.loads(GroundedGenerator(Transport(""))._user("如何随访？", [evidence()]))["evidence"][0]
    assert entry["text"] == "高血压患者应规律随访。"
    assert "chunk_ids" not in entry


def test_excerpt_can_be_verified_against_the_parent_context():
    chunk = Chunk("c1", "doc", "guide.md", "子块只写随访。", "guide.md", "管理", 2, 0,
                  parent_id="p1", parent_text="子块只写随访。完整句子：每年随访四次。",
                  parent_ordinal=0, token_count=8)
    transport = Transport(json.dumps({"claims": [{"text": "每年随访四次", "citations": [
        {"chunk_id": "c1", "excerpt": "每年随访四次"}]}]}))
    result = GroundedGenerator(transport).answer("如何随访？", [Evidence(chunk, 0.9)], "qwen")
    assert result["abstained"] is False
    assert result["citations"][0]["excerpt"] == "每年随访四次"


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
])
def test_invalid_citation_or_response_abstains(response):
    result = GroundedGenerator(Transport(response)).answer("问题", [evidence()], "qwen")
    assert result["abstained"] is True
    assert result["citations"] == []


def test_claim_is_anchored_by_chunk_id_without_a_verbatim_excerpt():
    """逐字摘录不再是必需项：只要锚到真实块，结论就成立，摘录只是可选展示。"""
    transport = Transport(json.dumps({"claims": [
        {"text": "应规律随访", "citations": [{"chunk_id": "block1"}]}]}))
    result = GroundedGenerator(transport).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["answer"] == "应规律随访 [block1]"
    assert "excerpt" not in result["citations"][0]


def test_claim_may_anchor_to_several_blocks():
    chunks = [
        Chunk("block1", "doc", "guide.md", "高血压患者应规律随访。", "guide.md", "管理", 2, 0),
        Chunk("block2", "doc", "guide.md", "随访频率取决于血压是否达标。", "guide.md", "管理", 3, 1),
    ]
    transport = Transport(json.dumps({"claims": [
        {"text": "随访频率应结合血压控制情况确定",
         "citations": [{"chunk_id": "block1"}, {"chunk_id": "block2"}, {"chunk_id": "block1"}]}]}))
    result = GroundedGenerator(transport).answer(
        "如何管理？", [Evidence(chunk, 0.9) for chunk in chunks], "qwen")

    assert result["abstained"] is False
    assert result["answer"] == "随访频率应结合血压控制情况确定 [block1] [block2]"
    assert [item["chunk_id"] for item in result["citations"]] == ["block1", "block2"]


def test_excerpt_that_does_not_match_is_dropped_but_the_anchor_survives():
    """摘录对不上原文只丢摘录，不丢结论——这正是取消强制逐字摘录后的语义。"""
    transport = Transport(json.dumps({"claims": [
        {"text": "应规律随访",
         "citations": [{"chunk_id": "block1", "excerpt": "不存在的原文"}]}]}))
    result = GroundedGenerator(transport).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["answer"] == "应规律随访 [block1]"
    assert "excerpt" not in result["citations"][0]


def test_uncovered_points_are_published_after_the_anchored_claims():
    transport = Transport(json.dumps({
        "claims": [{"text": "应规律随访", "citations": [{"chunk_id": "block1"}]}],
        "gaps": ["合并糖尿病患者的个体化目标", "当前资料未覆盖：儿童患者的随访频率", "合并糖尿病患者的个体化目标"],
    }, ensure_ascii=False))
    result = GroundedGenerator(transport).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["answer"] == ("应规律随访 [block1]\n"
                                "当前资料未覆盖：合并糖尿病患者的个体化目标\n"
                                "当前资料未覆盖：儿童患者的随访频率")
    assert result["gaps"] == ["合并糖尿病患者的个体化目标", "儿童患者的随访频率"]


def test_transport_requires_provider_key_before_network():
    transport = OpenAITransport(Settings(qwen_api_key=""))
    with pytest.raises(ApiError, match="API Key"):
        transport.complete("qwen", "system", "user")


def test_transport_sends_any_provider_label_to_the_configured_model():
    requests, clients = [], []

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            return type("Response", (), {"choices": [type("Choice", (), {
                "message": type("Message", (), {"content": "{}"})()})()]})()

    class Client:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.chat = type("Chat", (), {"completions": Completions()})()

    transport = OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client)
    assert transport.complete("qwen", "system", "user") == "{}"
    assert transport.complete("deepseek", "system", "user") == "{}"
    assert [item["model"] for item in requests] == ["qwen3.8-flash", "qwen3.8-flash"]
    assert clients[0]["max_retries"] == 0
    # The Qwen endpoint keeps the thinking flag that keeps generation inside the timeout.
    assert requests[0]["extra_body"] == {"enable_thinking": False}


@pytest.mark.parametrize("model,url,sends_flag", [
    ("qwen3.8-flash", "https://api.deepseek.com/v1", True),
    ("deepseek-chat", "https://dashscope.aliyuncs.com/compatible-mode/v1", True),
    ("Qwen3-32B-Instruct", "http://127.0.0.1:8001/v1", True),
    ("deepseek-chat", "https://api.deepseek.com/v1", False),
    ("gpt-4o-mini", "https://api.openai.com/v1", False),
])
def test_qwen_only_thinking_flag_follows_the_endpoint(model, url, sends_flag):
    assert ("extra_body" in completion_options(model, url)) is sends_flag


def test_configuration_probe_sends_the_same_options_as_generation():
    requests = []

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))])

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    verify_connection("gpt-4o-mini", "https://api.openai.com/v1", "key", client_factory=Client)
    verify_connection("qwen3.8-flash", "https://api.openai.com/v1", "key", client_factory=Client)
    assert requests[0]["max_tokens"] == 1 and "extra_body" not in requests[0]
    assert requests[1]["extra_body"] == {"enable_thinking": False}


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
    assert "优先 4–6 条" in transport.system
    assert "第 1 条直接回答问题" in transport.system
    # 锚点取代逐字摘录：结论依据是真实块 ID，一块说不全就多块。
    assert "chunk_id" in transport.system
    assert "一块或多块" in transport.system
    assert "excerpt 是可选的展示字段" in transport.system


def test_prompt_marks_uncovered_points_and_allows_controlled_hedging():
    transport = PromptTransport()
    GroundedGenerator(transport).draft("问题", [evidence()], "qwen")

    assert "gaps" in transport.system
    assert "当前资料未覆盖" in transport.system
    assert "可能" in transport.system and "也许" in transport.system
    assert "有依据的猜测" in transport.system
    # 模糊表述只允许出现在已锚定的资料范围内，且不得据此外推剂量与诊断。
    assert "已锚定的资料范围" in transport.system


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


class PromptTransport:
    def __init__(self):
        self.system = ""

    def complete(self, provider, system, user):
        self.system = system
        return '{"claims":[]}'


def test_prompt_states_the_medical_assistant_role_and_its_hard_requirements():
    """角色与四条硬要求来自产品定义的医疗助手提示词，必须原样留在系统提示词里。"""
    transport = PromptTransport()
    GroundedGenerator(transport).draft("问题", [evidence()], "qwen")

    assert "专业医疗知识助手" in transport.system
    assert "所有医学事实必须来源于检索资料" in transport.system
    assert "不得编造资料中不存在的疾病、药物、剂量和治疗方案" in transport.system
    assert "重新组织，而不是简单复制原文" in transport.system


def test_plan_section_is_empty_without_a_plan():
    assert plan_section("") == ""
    assert plan_section(None) == ""
    assert plan_section("   ") == ""
    # 非字符串（例如误连了别的端口）同样不得污染提示词。
    assert plan_section(["1. 基本概念解释"]) == ""


def test_draft_without_a_plan_keeps_the_prompt_free_of_structure_directives():
    transport = PromptTransport()
    GroundedGenerator(transport).draft("问题", [evidence()], "qwen")

    assert "回答结构规划" not in transport.system


def test_draft_follows_the_plan_connected_from_the_planning_node():
    transport = PromptTransport()
    GroundedGenerator(transport).draft("问题", [evidence()], "qwen",
                                       plan="1. 基本概念解释\n2. 常见处理方式")

    assert "回答结构规划" in transport.system
    assert "1. 基本概念解释" in transport.system
    assert "2. 常见处理方式" in transport.system
    # 覆盖驱动：条数由规划要点与要点下的事实数决定，而不是沿用"三五条"的保守默认。
    assert "至少由一条结论覆盖" in transport.system
    assert "分别写成几条结论" in transport.system
    # 分析段是延伸内容的主通道：规划在场时明确要求逐要点延伸并写足篇幅。
    assert "逐要点延伸" in transport.system
    # 结构规划只编排结论，锚定等硬约束不因它放宽。
    assert "最多 8 条" in transport.system
    assert "一块或多块" in transport.system


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


def test_validator_keeps_claims_anchored_to_retrieved_blocks_when_another_claim_is_unanchored():
    output = json.dumps({"claims": [
        {"text": "应规律随访", "citations": [{"chunk_id": "block1"}]},
        {"text": "未经支持的结论", "citations": [{"chunk_id": "not-retrieved"}]},
    ]})

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["answer"].startswith("应规律随访 [block1]")
    assert "未经支持的结论" not in result["answer"]


def test_validator_abstains_when_all_claims_name_blocks_outside_the_evidence():
    output = json.dumps({"claims": [
        {"text": "未经支持的结论", "citations": [{"chunk_id": "not-retrieved"}]},
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
    # answer 仍然只含锚定校验过的结论，评测与旧调用方拿到的一模一样。
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


def test_analysis_never_becomes_a_channel_around_anchor_validation():
    """结论全部没有锚点时，附带的解释段必须一起丢掉，否则等于绕过锚点校验。"""
    output = json.dumps({"claims": [{"text": "伪造结论", "citations": [
        {"chunk_id": "not-retrieved"}]}],
        "analysis": {"summary": "这段解释本身读起来很合理。", "extensions": [],
                     "based_on": ["block1"]}}, ensure_ascii=False)

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    # 拒答原因必须写明是锚点校验失败，不能伪装成“资料不足”。
    assert result == {"answer": ABSTENTION, "citations": [], "abstained": True,
                      "abstain_reason": ABSTAIN_UNANCHORED}


@pytest.mark.parametrize("analysis", [
    # 形状不对的一律丢弃，不做修补（based_on 留空、填错或形状错时走锚定回退，另有测试）。
    {"summary": "", "extensions": [], "based_on": ["block1"]},
    {"summary": "   ", "extensions": [], "based_on": ["block1"]},
    {"summary": 123, "extensions": [], "based_on": ["block1"]},
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


@pytest.mark.parametrize("based_on", [
    [],                                       # 模型留空
    ["other"],                                # 指向了未引用块
    ["never-cited", "other"],                 # 全部指向未引用块
    [1, None],                                # 元素形状错误
])
def test_unusable_based_on_falls_back_to_cited_blocks_in_citation_order(based_on):
    """based_on 只是展示元数据：模型没写出任何可用锚点时锚到已引用块，分析段不静默消失。"""
    chunks = [
        Chunk("block1", "doc", "guide.md", "高血压患者应规律随访。", "guide.md", "管理", 2, 0),
        Chunk("block2", "doc", "guide.md", "血压需要长期管理。", "guide.md", "管理", 3, 1),
    ]
    output = json.dumps({
        "claims": [
            {"text": "需要长期管理", "citations": [{"chunk_id": "block2", "excerpt": "长期管理"}]},
            {"text": "应规律随访", "citations": [{"chunk_id": "block1", "excerpt": "规律随访"}]},
        ],
        "analysis": {"summary": "把两条结论串起来看，随访与长期管理是同一件事。",
                     "extensions": [], "based_on": based_on},
    }, ensure_ascii=False)

    result = GroundedGenerator(Transport(output)).answer(
        "如何管理？", [Evidence(chunk, 0.9) for chunk in chunks], "qwen")

    assert result["abstained"] is False
    # 锚点顺序跟结论首次引用的顺序一致（block2 先被引用），而不是集合的哈希序。
    assert result["analysis"]["based_on"] == ["block2", "block1"]


@pytest.mark.parametrize("summary", [
    # 复述已验证结论里的数值是安全的，必须放行；按字面禁数字会把这段整段删掉。
    "血压目标与指南给出的数值一致，需要长期坚持。",
    "通常需要按固定间隔复查。",
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


def test_analysis_with_empty_based_on_anchors_to_every_cited_block():
    """模型漏填或留空 based_on 时分析段不再整段静默消失：锚到本次全部已引用块。"""
    chunks = [
        Chunk("block1", "doc", "guide.md", "高血压患者应规律随访。", "guide.md", "管理", 2, 0),
        Chunk("block2", "doc", "guide.md", "血压需要长期管理。", "guide.md", "管理", 3, 1),
    ]
    output = json.dumps({
        "claims": [
            {"text": "应规律随访", "citations": [{"chunk_id": "block1", "excerpt": "规律随访"}]},
            {"text": "需要长期管理", "citations": [{"chunk_id": "block2", "excerpt": "长期管理"}]},
        ],
        "analysis": {"summary": "把两条结论串起来看，随访与长期管理是同一件事。",
                     "extensions": [], "based_on": []},
    }, ensure_ascii=False)

    result = GroundedGenerator(Transport(output)).answer(
        "如何管理？", [Evidence(chunk, 0.9) for chunk in chunks], "qwen")

    assert result["abstained"] is False
    assert result["analysis"]["based_on"] == ["block1", "block2"]


def test_analysis_keeps_only_valid_extensions_up_to_the_cap():
    output = analysis_document(extensions=[
        "随访便于医生掌握病情变化。", "", "记录用药后的感受。",
        "复诊时带上既往检查资料。", "出现不适及时就医。", "注意监测血压变化。"])

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    # 空串被丢，其余按原顺序保留；上限按"通过校验的条数"计算，不因丢条而少给。
    assert result["analysis"]["extensions"] == [
        "随访便于医生掌握病情变化。", "记录用药后的感受。", "复诊时带上既往检查资料。",
        "出现不适及时就医。", "注意监测血压变化。"]


def test_fenced_json_is_answered_instead_of_reported_as_insufficient_evidence():
    """模型加 Markdown 围栏是格式问题，不是资料问题：答案必须照常产出。"""
    output = "```json\n" + json.dumps({"claims": [{"text": "应规律随访", "citations": [
        {"chunk_id": "block1", "excerpt": "规律随访"}]}]}, ensure_ascii=False) + "\n```"

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["answer"] == "应规律随访 [block1]"
    # 修复痕迹要能被运维看到，否则“模型输出不合规”这件事仍然是隐形的。
    assert result["output_repaired"] == "fence"


def test_json_with_surrounding_explanation_is_answered():
    output = ("好的，以下是我根据资料整理的回答：\n"
              + json.dumps({"claims": [{"text": "应规律随访", "citations": [
                  {"chunk_id": "block1"}]}]}, ensure_ascii=False)
              + "\n如果还有疑问可以继续提问。")

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["answer"] == "应规律随访 [block1]"
    assert result["output_repaired"] == "prose"


def test_truncated_json_keeps_the_claims_that_were_complete():
    """被 max_tokens 截断时，已经写完的结论仍然锚定有效，不能整段丢成“资料不足”。"""
    output = ('{"claims":[{"text":"应规律随访","citations":[{"chunk_id":"block1"}]},'
              '{"text":"第二条结论写到一半')

    result = GroundedGenerator(Transport(output)).answer("如何管理？", [evidence()], "qwen")

    assert result["abstained"] is False
    assert result["answer"] == "应规律随访 [block1]"
    assert result["output_repaired"] == "truncated"


def test_unparsable_output_names_a_parse_error_not_an_evidence_shortage():
    result = GroundedGenerator(Transport("no json at all")).answer(
        "如何管理？", [evidence()], "qwen")

    assert result["abstained"] is True
    assert result["citations"] == []
    assert result["abstain_reason"] == ABSTAIN_PARSE_ERROR
    # 用户看到的文案不能说成“资料不足”，那会把生成故障伪装成检索故障。
    assert result["answer"] != ABSTENTION
    assert "格式" in result["answer"]


def test_truncated_output_with_nothing_recoverable_names_truncation():
    output = '{"claims":[{"text":"半条'

    result = GroundedGenerator(Transport(output)).validate(output, [evidence()], truncated=True)

    assert result["abstained"] is True
    assert result["abstain_reason"] == ABSTAIN_TRUNCATED
    assert result["truncated"] is True
    assert result["answer"] != ABSTENTION
    assert "截断" in result["answer"]


def test_recovered_but_truncated_answer_is_marked_for_callers():
    output = ('{"claims":[{"text":"应规律随访","citations":[{"chunk_id":"block1"}]}],'
              '"analysis":{"summary":"写到一半')

    result = GroundedGenerator(Transport(output)).validate(output, [evidence()], truncated=True)

    assert result["abstained"] is False
    assert result["output_repaired"] == "truncated"
    assert result["truncated"] is True


def test_grounded_completion_sends_an_explicit_budget_and_json_mode():
    requests, outcomes = [], [("{}", "stop")]
    transport = OpenAITransport(Settings(qwen_api_key="key"),
                                client_factory=_completion_client(requests, outcomes))

    completion = transport.complete_grounded("qwen", "system", "user")

    assert completion.text == "{}" and completion.truncated is False
    assert requests[0]["max_tokens"] == MAX_OUTPUT_TOKENS
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["extra_body"] == {"enable_thinking": False}


def test_grounded_completion_retries_once_with_a_larger_budget_when_truncated():
    requests, outcomes = [], [('{"claims":[{"text":"写到一半', "length"),
                              ('{"claims":[]}', "stop")]
    transport = OpenAITransport(Settings(qwen_api_key="key"),
                                client_factory=_completion_client(requests, outcomes))

    completion = transport.complete_grounded("qwen", "system", "user")

    assert completion.text == '{"claims":[]}' and completion.truncated is False
    assert [item["max_tokens"] for item in requests] == [MAX_OUTPUT_TOKENS,
                                                         TRUNCATION_RETRY_MAX_TOKENS]


def test_grounded_completion_keeps_the_truncated_prefix_when_the_retry_is_refused():
    upstream = ApiError("MODEL_UPSTREAM_ERROR", "生成模型请求失败，请稍后重试", 502)
    requests, outcomes = [], [('{"claims":[{"text":"写到一半', "length"), upstream]
    transport = OpenAITransport(Settings(qwen_api_key="key"),
                                client_factory=_completion_client(requests, outcomes))

    completion = transport.complete_grounded("qwen", "system", "user")

    # 重试被拒绝时保留第一次的可恢复前缀，交给解析器抢救，而不是让整次回答失败。
    assert completion.truncated is True
    assert completion.text == '{"claims":[{"text":"写到一半'


def test_grounded_completion_falls_back_when_response_format_is_rejected_once():
    request = httpx.Request("POST", "https://example.invalid/chat")
    rejected = APIStatusError("unsupported", response=httpx.Response(400, request=request),
                              body=None)
    requests, outcomes = [], [rejected, ("{}", "stop"), ("{}", "stop")]
    transport = OpenAITransport(Settings(qwen_api_key="key"),
                                client_factory=_completion_client(requests, outcomes))

    assert transport.complete_grounded("qwen", "system", "user").text == "{}"
    assert "response_format" not in requests[1]
    # 端点被记住：下一次回答不再多发一次注定被拒的请求。
    assert transport.complete_grounded("qwen", "system", "user").text == "{}"
    assert len(requests) == 3
    assert "response_format" not in requests[2]


def test_generation_reports_truncation_from_the_transport_to_the_validator():
    class TruncatedTransport:
        def complete_grounded(self, provider, system, user):
            return Completion('{"claims":[{"text":"写不下', "length")

    generator = GroundedGenerator(TruncatedTransport())
    produced = generator.generate("如何管理？", [evidence()], "qwen")

    assert produced["truncated"] is True
    assert generator.validate(produced["draft"], produced["evidence"],
                              truncated=produced["truncated"])["abstain_reason"] == ABSTAIN_TRUNCATED


def _completion_client(requests, outcomes):
    """Fake OpenAI client returning queued ``(content, finish_reason)`` outcomes or raising."""

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            content, finish_reason = outcome
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason)])

    class Client:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    return Client
