"""Final evidence must remain a real, valid Evidence snapshot."""

from dataclasses import replace
from time import monotonic

import pytest
from test_agentic_workflow import item

from medical_rag.workflow.agentic_runtime import AgentRuntime
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.definition import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)
from medical_rag.workflow.engine import WorkflowEngine
from medical_rag.workflow.nodes.base import BuiltinNode
from medical_rag.workflow.nodes.retrieval import rerank, rrf
from medical_rag.workflow.registry import NodeRegistry


@pytest.mark.parametrize("limit", [2, 5])
def test_ordinary_reranking_scores_only_the_rrf_head(limit):
    context = ExecutionContext("run", "ordinary", {})
    candidates = [
        replace(
            item(str(index)), chunk=replace(item(str(index)).chunk, text=str(index))
        )
        for index in range(10)
    ]
    fused = rrf({"sources": [candidates, candidates[::-1]]}, {}, context)["evidence"]

    class Reranker:
        def score(self, question, texts):
            assert question == "original"
            assert texts == [e.chunk.text for e in fused[: max(3, limit)]]
            return list(range(len(texts)))

    result = rerank(
        {"question": "original", "evidence": fused},
        {"top_k": limit},
        context,
        Reranker(),
        0,
        3,
        5,
    )
    assert [e.chunk.id for e in result["evidence"]] == [
        e.chunk.id for e in reversed(fused[: max(3, limit)])
    ][:limit]


def test_correction_pool_respects_wired_evidence_and_reconsiders_history():
    runtime = AgentRuntime("original", monotonic() + 60, 2, "agentic")
    runtime.evidence = [item("old-hidden"), item("old-wired"), item("old-wired")]
    runtime.retrieved = [item("fresh-hidden"), item("fresh-wired"), item("fresh-wired")]
    context = ExecutionContext("run", "child", {"_agent_runtime": runtime})
    wired = [
        replace(item(key), chunk=replace(item(key).chunk, text=key))
        for key in ("historical", "old-wired", "fresh-wired")
    ]

    class Reranker:
        def score(self, question, texts):
            # No hidden/disconnected evidence or duplicate IDs can enter the pool.
            assert texts == ["old-wired", "fresh-wired", "historical"]
            return [0.8, 0.9, 0.7]

    result = rerank(
        {"question": "original", "evidence": wired + wired[-1:]},
        {"top_k": 2},
        context,
        Reranker(),
        0,
        3,
        5,
    )
    assert [e.chunk.id for e in result["evidence"]] == ["fresh-wired", "old-wired"]


@pytest.mark.asyncio
async def test_malformed_output_cannot_replace_valid_evidence_snapshot():
    registry = NodeRegistry()
    registry.register(
        BuiltinNode(
            "test.seed",
            "seed",
            "input",
            {},
            {"evidence": "evidence_list"},
            lambda inputs, config, context: {"evidence": [item("real")]},
        )
    )
    registry.register(
        BuiltinNode(
            "test.output",
            "output",
            "output",
            {"evidence": "evidence_list"},
            {"evidence": "evidence_list"},
            lambda inputs, config, context: {"evidence": ["fabricated"]},
        )
    )
    graph = WorkflowDefinition(
        id="bad-output",
        name="bad-output",
        version=1,
        status="draft",
        nodes=[
            WorkflowNode(id="seed", type="test.seed"),
            WorkflowNode(id="output", type="test.output"),
        ],
        edges=[
            WorkflowEdge(
                id="edge",
                source="seed",
                source_port="evidence",
                target="output",
                target_port="evidence",
            )
        ],
    )
    result = await WorkflowEngine(registry).run(graph, {})
    assert result.evidence == [item("real")]
