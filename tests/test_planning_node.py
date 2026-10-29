import json

import pytest

from medical_rag.types import ApiError, Chunk, Evidence
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.nodes.planning import STANDARD_SECTIONS, plan


CONTEXT = ExecutionContext("run", "medical-answer", {})
QUESTION = {"question": "高血压如何长期管理？", "provider": "qwen"}


class Transport:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def complete(self, provider, system, user):
        self.calls.append((provider, system, json.loads(user)))
        return self.reply


def test_standard_preset_renders_the_fixed_medical_structure_without_a_model_call():
    transport = Transport("{}")
    produced = plan(QUESTION, {"preset": "standard"}, CONTEXT, transport, "qwen")

    assert produced["plan"].splitlines() == [
        f"{index}. {name}" for index, name in enumerate(STANDARD_SECTIONS, 1)]
    assert transport.calls == []


def test_off_preset_publishes_no_structure_so_generation_keeps_its_plain_prompt():
    produced = plan(QUESTION, {"preset": "off"}, CONTEXT, Transport("{}"), "qwen")

    assert produced == {"plan": ""}


def test_auto_preset_asks_the_model_and_relays_sections_in_order():
    transport = Transport(json.dumps({"sections": ["处理方式", "注意事项"]}, ensure_ascii=False))

    produced = plan(QUESTION, {}, CONTEXT, transport, "qwen")

    assert produced["plan"] == "1. 处理方式\n2. 注意事项"
    provider, system, payload = transport.calls[0]
    assert provider == "qwen"
    assert "sections" in system
    assert payload["question"] == QUESTION["question"]
    # 没连证据时规划者只看问题；资料主题为空列表，提示词仍声明其为不可信数据。
    assert payload["material"] == []


def test_auto_preset_sees_material_topics_from_connected_evidence():
    chunk = Chunk("c1", "d1", "guide.md", "高血压患者应规律随访。", "guide.md", "治疗", 1, 0)
    transport = Transport('{"sections":["处理方式","注意事项"]}')

    plan({**QUESTION, "evidence": [Evidence(chunk, 0.9)]}, {}, CONTEXT, transport, "qwen")

    assert transport.calls[0][2]["material"] == ["guide.md · 治疗"]


@pytest.mark.parametrize("reply", [
    "not json",
    "{}",
    '{"sections":[]}',
    '{"sections":["只有一条"]}',
    json.dumps({"sections": ["第一条", {"不是": "字符串"}]}, ensure_ascii=False),
    json.dumps({"sections": ["第一条", "字" * 21]}, ensure_ascii=False),
])
def test_auto_preset_falls_back_to_the_standard_structure_on_unusable_replies(reply):
    produced = plan(QUESTION, {}, CONTEXT, Transport(reply), "qwen")

    assert produced["plan"].startswith("1. 基本概念解释")
    assert "5. 注意事项" in produced["plan"]


def test_transport_failure_fails_the_node_like_other_model_nodes():
    """模型调用失败与其他模型节点同责：规划是增强，但不可用必须显式失败，不悄悄换骨架。"""

    class BrokenTransport:
        def complete(self, provider, system, user):
            raise ApiError("MODEL_RATE_LIMITED", "稍后重试", 429)

    with pytest.raises(ApiError, match="稍后重试"):
        plan(QUESTION, {}, CONTEXT, BrokenTransport(), "qwen")


def test_auto_preset_runs_without_a_provider_wire():
    transport = Transport('{"sections":["处理方式","注意事项"]}')

    plan({"question": "高血压如何长期管理？"}, {}, CONTEXT, transport, "qwen")

    assert transport.calls[0][0] == "qwen"
