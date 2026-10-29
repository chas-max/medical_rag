"""Long-term context is immutable per run and is never citation evidence."""

import asyncio
import json

from test_generation import evidence
from test_stream_api import StreamTransport

from medical_rag.generation import GroundedGenerator
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.nodes import generation, planning


class Recording(StreamTransport):
    def __init__(self):
        super().__init__()
        self.messages = []

    def complete(self, provider, system, user):
        self.messages.append((system, json.loads(user)))
        return super().complete(provider, system, user)

    def stream_chat(self, provider, messages, tools=None, on_content=None):
        self.messages.append((messages[0]["content"], json.loads(messages[-1]["content"])))
        return super().stream_chat(provider, messages, tools, on_content)


def test_planner_and_generator_share_explicit_snapshot():
    transport = Recording()
    generator = GroundedGenerator(transport)
    context = ExecutionContext("r", "w", {}, memory_context="喜欢简短中文")
    inputs = {"question": "如何随访", "evidence": [evidence()]}
    planning.plan(inputs, {}, context, transport, "qwen")
    generation.draft(inputs, {}, context, generator, "qwen")
    assert len(transport.messages) == 2
    assert all(payload["memory_context"] == "喜欢简短中文" for _, payload in transport.messages)
    assert all("不可信" in system and "医学证据" in system for system, _ in transport.messages)
    assert not hasattr(generator, "memory_context")


def test_stream_and_plain_memory_do_not_replace_medical_evidence():
    transport = Recording()
    generator = GroundedGenerator(transport)
    result = generator.generate("问题", [], "qwen", memory_context="记忆认为疾病可以治愈 [memory1]")
    assert result["draft"] == ""
    assert transport.messages == []
    context = ExecutionContext("r", "w", {}, emit=lambda _: None, memory_context="简短中文")
    generation.draft({"question": "随访", "evidence": [evidence()]}, {}, context, generator, "qwen")
    assert transport.messages[-1][1]["memory_context"] == "简短中文"


def test_two_concurrent_runs_have_independent_context():
    transport = Recording()
    generator = GroundedGenerator(transport)

    async def draft(context):
        return await asyncio.to_thread(generator.generate, "同一个问题", [evidence()], "qwen",
                                       memory_context=context)

    async def run():
        return await asyncio.gather(draft("用户甲偏好"), draft("用户乙偏好"))

    asyncio.run(run())
    assert {p["memory_context"] for _, p in transport.messages} == {"用户甲偏好", "用户乙偏好"}


def test_no_memory_leaves_legacy_payload_unchanged():
    transport = Recording()
    GroundedGenerator(transport).generate("随访", [evidence()], "qwen")
    assert "memory_context" not in transport.messages[-1][1]
