import re
from dataclasses import dataclass, field
from typing import Callable

import pytest

from medical_rag.types import ApiError
from medical_rag.workflow.contracts import ExecutionContext, NodeResult
from medical_rag.workflow.definition import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)
from medical_rag.workflow.engine import WorkflowEngine
from medical_rag.workflow.registry import NodeRegistry


NodeFunction = Callable[[dict[str, object], ExecutionContext], NodeResult]


@dataclass
class FunctionalNode:
    type: str
    title: str
    category: str
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    config_schema: dict[str, object] = field(default_factory=dict)
    function: NodeFunction | None = None

    async def execute(
        self,
        inputs: dict[str, object],
        config: dict[str, object],
        context: ExecutionContext,
    ) -> NodeResult:
        if self.function is not None:
            return self.function(inputs, context)
        return NodeResult(outputs=dict(inputs), status="success")


def make_definition(
    nodes: list[dict[str, object]], edges: list[dict[str, str]]
) -> WorkflowDefinition:
    return WorkflowDefinition(
        id="demo",
        name="Demo",
        version=1,
        status="draft",
        nodes=[WorkflowNode.model_validate(node) for node in nodes],
        edges=[WorkflowEdge.model_validate(edge) for edge in edges],
    )


def make_registry(*nodes: FunctionalNode) -> NodeRegistry:
    registry = NodeRegistry()
    for node in nodes:
        registry.register(node)
    return registry


@pytest.mark.asyncio
async def test_engine_runs_branch_and_join_in_topological_order() -> None:
    order: list[str] = []

    def run(name: str, output: dict[str, object]) -> NodeFunction:
        def execute(inputs: dict[str, object], context: ExecutionContext) -> NodeResult:
            order.append(name)
            if name == "query":
                return NodeResult(
                    outputs={"question": inputs["question"]}, status="success"
                )
            if name == "fusion":
                assert inputs == {"dense": ["d"], "bm25": ["b"]}
            return NodeResult(outputs=output, status="success")

        return execute

    registry = make_registry(
        FunctionalNode(
            "query.input",
            "Query",
            "input",
            outputs={"question": "string"},
            function=run("query", {}),
        ),
        FunctionalNode(
            "retrieval.dense",
            "Dense",
            "retrieval",
            inputs={"question": "string"},
            outputs={"hits": "evidence"},
            function=run("dense", {"hits": ["d"]}),
        ),
        FunctionalNode(
            "retrieval.bm25",
            "BM25",
            "retrieval",
            inputs={"question": "string"},
            outputs={"hits": "evidence"},
            function=run("bm25", {"hits": ["b"]}),
        ),
        FunctionalNode(
            "retrieval.fusion",
            "Fusion",
            "retrieval",
            inputs={"dense": "evidence", "bm25": "evidence"},
            outputs={"answer": "string"},
            function=run("fusion", {"answer": "ok"}),
        ),
        FunctionalNode(
            "answer.output",
            "Output",
            "output",
            inputs={"answer": "string"},
            outputs={"answer": "string"},
            function=run("output", {"answer": "ok"}),
        ),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "dense", "type": "retrieval.dense"},
            {"id": "bm25", "type": "retrieval.bm25"},
            {"id": "fusion", "type": "retrieval.fusion"},
            {"id": "output", "type": "answer.output"},
        ],
        [
            {
                "id": "e1",
                "source": "query",
                "source_port": "question",
                "target": "dense",
                "target_port": "question",
            },
            {
                "id": "e2",
                "source": "query",
                "source_port": "question",
                "target": "bm25",
                "target_port": "question",
            },
            {
                "id": "e3",
                "source": "dense",
                "source_port": "hits",
                "target": "fusion",
                "target_port": "dense",
            },
            {
                "id": "e4",
                "source": "bm25",
                "source_port": "hits",
                "target": "fusion",
                "target_port": "bm25",
            },
            {
                "id": "e5",
                "source": "fusion",
                "source_port": "answer",
                "target": "output",
                "target_port": "answer",
            },
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "高血压"})

    assert result.status == "success"
    assert result.output == {"answer": "ok"}
    assert order == ["query", "dense", "bm25", "fusion", "output"]
    assert [trace.node_id for trace in result.trace] == order
    assert result.trace[-1].status == "success"
    assert all(trace.duration_ms >= 0 for trace in result.trace)
    assert re.fullmatch(r"[0-9a-f]{32}", result.run_id)


@pytest.mark.asyncio
async def test_engine_skips_node_when_required_upstream_output_is_absent() -> None:
    called: list[str] = []
    registry = make_registry(
        FunctionalNode(
            "query.input",
            "Query",
            "input",
            outputs={"question": "string"},
            function=lambda inputs, context: NodeResult(outputs={}, status="success"),
        ),
        FunctionalNode(
            "generation.llm",
            "Generation",
            "generation",
            inputs={"question": "string"},
            outputs={"answer": "string"},
            function=lambda inputs, context: called.append("generation")
            or NodeResult(outputs={"answer": "bad"}, status="success"),
        ),
        FunctionalNode(
            "answer.output",
            "Output",
            "output",
            inputs={"answer": "string"},
            outputs={"answer": "string"},
        ),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "generation", "type": "generation.llm"},
            {"id": "output", "type": "answer.output"},
        ],
        [
            {
                "id": "e1",
                "source": "query",
                "source_port": "question",
                "target": "generation",
                "target_port": "question",
            },
            {
                "id": "e2",
                "source": "generation",
                "source_port": "answer",
                "target": "output",
                "target_port": "answer",
            },
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "问题"})

    assert called == []
    assert result.trace_by_node("generation").status == "skipped"
    assert result.trace_by_node("output").status == "skipped"


@pytest.mark.asyncio
async def test_engine_skips_generation_after_evidence_gate_abstains() -> None:
    order: list[str] = []

    def result_for(name: str, outputs: dict[str, object]) -> NodeFunction:
        def execute(inputs: dict[str, object], context: ExecutionContext) -> NodeResult:
            order.append(name)
            return NodeResult(outputs=outputs, status="success")

        return execute

    registry = make_registry(
        FunctionalNode(
            "query.input",
            "Query",
            "input",
            outputs={"question": "string"},
            function=result_for("query", {"question": "未知问题"}),
        ),
        FunctionalNode(
            "evidence.gate",
            "Gate",
            "evidence",
            inputs={"question": "string"},
            outputs={"answer": "string", "abstained": "boolean"},
            function=result_for("gate", {"answer": "资料不足", "abstained": True}),
        ),
        FunctionalNode(
            "generation.llm",
            "Generation",
            "generation",
            inputs={"answer": "string"},
            outputs={"answer": "string"},
            function=result_for("generation", {"answer": "不应生成"}),
        ),
        FunctionalNode(
            "answer.output",
            "Output",
            "output",
            inputs={"answer": "string"},
            outputs={"answer": "string"},
        ),
        FunctionalNode(
            "audit.record",
            "Audit",
            "audit",
            inputs={"question": "string"},
            outputs={"recorded": "boolean"},
            function=result_for("audit", {"recorded": True}),
        ),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "gate", "type": "evidence.gate"},
            {"id": "generation", "type": "generation.llm"},
            {"id": "output", "type": "answer.output"},
            {"id": "audit", "type": "audit.record"},
        ],
        [
            {
                "id": "e1",
                "source": "query",
                "source_port": "question",
                "target": "gate",
                "target_port": "question",
            },
            {
                "id": "e2",
                "source": "gate",
                "source_port": "answer",
                "target": "generation",
                "target_port": "answer",
            },
            {
                "id": "e3",
                "source": "generation",
                "source_port": "answer",
                "target": "output",
                "target_port": "answer",
            },
            {
                "id": "e4",
                "source": "query",
                "source_port": "question",
                "target": "audit",
                "target_port": "question",
            },
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "未知问题"})

    assert result.status == "success"
    assert result.output == {"answer": "资料不足", "abstained": True}
    assert "generation" not in order
    assert "audit" in order
    assert result.trace_by_node("generation").status == "skipped"
    assert result.trace_by_node("output").status == "skipped"
    assert result.trace_by_node("audit").status == "success"


@pytest.mark.asyncio
async def test_engine_stops_after_failed_node() -> None:
    called: list[str] = []
    registry = make_registry(
        FunctionalNode(
            "query.input",
            "Query",
            "input",
            outputs={"question": "string"},
            function=lambda inputs, context: NodeResult(
                outputs={"question": "q"}, status="success"
            ),
        ),
        FunctionalNode(
            "worker.fail",
            "Fail",
            "transform",
            inputs={"question": "string"},
            outputs={"value": "string"},
            function=lambda inputs, context: called.append("fail")
            or NodeResult(
                outputs={}, status="failed", error={"code": "FAILED", "message": "safe"}
            ),
        ),
        FunctionalNode(
            "answer.output",
            "Output",
            "output",
            inputs={"value": "string"},
            function=lambda inputs, context: called.append("output")
            or NodeResult(outputs=inputs, status="success"),
        ),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "failure", "type": "worker.fail"},
            {"id": "output", "type": "answer.output"},
        ],
        [
            {
                "id": "e1",
                "source": "query",
                "source_port": "question",
                "target": "failure",
                "target_port": "question",
            },
            {
                "id": "e2",
                "source": "failure",
                "source_port": "value",
                "target": "output",
                "target_port": "value",
            },
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {})

    assert result.status == "failed"
    assert called == ["fail"]
    assert [trace.node_id for trace in result.trace] == ["query", "failure"]
    assert result.trace[-1].status == "failed"
    assert result.trace[-1].error == {
        "code": "NODE_EXECUTION_ERROR",
        "message": "节点执行失败",
    }


@pytest.mark.asyncio
async def test_engine_converts_api_and_unexpected_errors_to_safe_trace_data() -> None:
    registry = make_registry(
        FunctionalNode(
            "api.error",
            "API Error",
            "output",
            function=lambda inputs, context: (_ for _ in ()).throw(
                ApiError("NOT_READY", "服务尚未初始化", 503)
            ),
        ),
        FunctionalNode(
            "runtime.error",
            "Runtime Error",
            "output",
            function=lambda inputs, context: (_ for _ in ()).throw(
                RuntimeError("token=secret-value")
            ),
        ),
    )

    for node_id, node_type, expected in (
        ("api", "api.error", {"code": "NOT_READY", "message": "服务尚未初始化"}),
        (
            "runtime",
            "runtime.error",
            {"code": "NODE_EXECUTION_ERROR", "message": "节点执行失败"},
        ),
    ):
        definition = make_definition([{"id": node_id, "type": node_type}], [])
        result = await WorkflowEngine(registry).run(definition, {})

        assert result.status == "failed"
        assert result.trace_by_node(node_id).error == expected
        assert "secret-value" not in repr(result.trace)


@pytest.mark.asyncio
async def test_engine_does_not_expose_untrusted_node_result_error_strings() -> None:
    secret = "api_key=sk-node-secret"
    traceback = f"Traceback (most recent call last):\nRuntimeError: {secret}"
    registry = make_registry(
        FunctionalNode(
            "node.error",
            "Node Error",
            "output",
            function=lambda inputs, context: NodeResult(
                outputs={},
                status="failed",
                error={"code": secret, "message": traceback},
            ),
        )
    )
    definition = make_definition([{"id": "node", "type": "node.error"}], [])

    result = await WorkflowEngine(registry).run(definition, {})

    assert result.status == "failed"
    assert result.trace_by_node("node").error == {
        "code": "NODE_EXECUTION_ERROR",
        "message": "节点执行失败",
    }
    assert secret not in repr(result.trace)
    assert "Traceback" not in repr(result.trace)


@pytest.mark.asyncio
async def test_engine_collects_multiple_edges_into_a_list_for_the_same_port() -> None:
    registry = make_registry(
        FunctionalNode(
            "query.input",
            "Query",
            "input",
            outputs={"question": "string"},
            function=lambda inputs, context: NodeResult(
                outputs={"question": "q"}, status="success"
            ),
        ),
        FunctionalNode(
            "retrieval.one",
            "One",
            "retrieval",
            inputs={"question": "string"},
            outputs={"value": "string"},
            function=lambda inputs, context: NodeResult(
                outputs={"value": "one"}, status="success"
            ),
        ),
        FunctionalNode(
            "retrieval.two",
            "Two",
            "retrieval",
            inputs={"question": "string"},
            outputs={"value": "string"},
            function=lambda inputs, context: NodeResult(
                outputs={"value": "two"}, status="success"
            ),
        ),
        FunctionalNode(
            "join.node",
            "Join",
            "output",
            inputs={"values": "string"},
            function=lambda inputs, context: NodeResult(
                outputs={"values": inputs["values"]}, status="success"
            ),
        ),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "one", "type": "retrieval.one"},
            {"id": "two", "type": "retrieval.two"},
            {"id": "join", "type": "join.node"},
        ],
        [
            {
                "id": "e1",
                "source": "query",
                "source_port": "question",
                "target": "one",
                "target_port": "question",
            },
            {
                "id": "e2",
                "source": "query",
                "source_port": "question",
                "target": "two",
                "target_port": "question",
            },
            {
                "id": "e3",
                "source": "one",
                "source_port": "value",
                "target": "join",
                "target_port": "values",
            },
            {
                "id": "e4",
                "source": "two",
                "source_port": "value",
                "target": "join",
                "target_port": "values",
            },
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "q"})

    assert result.output == {"values": ["one", "two"]}


@pytest.mark.asyncio
async def test_engine_skips_join_when_any_required_fan_in_value_is_absent() -> None:
    called: list[str] = []
    registry = make_registry(
        FunctionalNode(
            "query.input",
            "Query",
            "input",
            outputs={"question": "string"},
            function=lambda inputs, context: NodeResult(
                outputs={"question": "q"}, status="success"
            ),
        ),
        FunctionalNode(
            "retrieval.one",
            "One",
            "retrieval",
            inputs={"question": "string"},
            outputs={"value": "string"},
            function=lambda inputs, context: NodeResult(
                outputs={"value": "one"}, status="success"
            ),
        ),
        FunctionalNode(
            "retrieval.two",
            "Two",
            "retrieval",
            inputs={"question": "string"},
            outputs={"value": "string"},
            function=lambda inputs, context: NodeResult(outputs={}, status="success"),
        ),
        FunctionalNode(
            "join.node",
            "Join",
            "output",
            inputs={"values": "string"},
            function=lambda inputs, context: called.append("join")
            or NodeResult(outputs={"values": inputs["values"]}, status="success"),
        ),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "one", "type": "retrieval.one"},
            {"id": "two", "type": "retrieval.two"},
            {"id": "join", "type": "join.node"},
        ],
        [
            {
                "id": "e1",
                "source": "query",
                "source_port": "question",
                "target": "one",
                "target_port": "question",
            },
            {
                "id": "e2",
                "source": "query",
                "source_port": "question",
                "target": "two",
                "target_port": "question",
            },
            {
                "id": "e3",
                "source": "one",
                "source_port": "value",
                "target": "join",
                "target_port": "values",
            },
            {
                "id": "e4",
                "source": "two",
                "source_port": "value",
                "target": "join",
                "target_port": "values",
            },
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "q"})

    assert called == []
    assert result.trace_by_node("join").status == "skipped"


@pytest.mark.asyncio
async def test_explicit_skipped_result_can_supply_join_placeholder() -> None:
    registry = make_registry(
        FunctionalNode(
            "query.input",
            "Input",
            "input",
            outputs={"route": "string"},
            function=lambda inputs, context: NodeResult({"route": "direct"}, "success"),
        ),
        FunctionalNode(
            "branch.skip",
            "Skip",
            "question",
            inputs={"route": "string"},
            outputs={"variant": "query_variant"},
            function=lambda inputs, context: NodeResult(
                {"variant": {"active": False}}, "skipped"
            ),
        ),
        FunctionalNode(
            "join.node",
            "Join",
            "output",
            inputs={"variant": "query_variant"},
            outputs={"answer": "string"},
            function=lambda inputs, context: NodeResult(
                {"answer": "original" if not inputs["variant"]["active"] else "changed"},
                "success",
            ),
        ),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "branch", "type": "branch.skip"},
            {"id": "join", "type": "join.node"},
        ],
        [
            {
                "id": "e1",
                "source": "query",
                "source_port": "route",
                "target": "branch",
                "target_port": "route",
            },
            {
                "id": "e2",
                "source": "branch",
                "source_port": "variant",
                "target": "join",
                "target_port": "variant",
            },
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {})

    assert result.output == {"answer": "original"}
    assert result.trace_by_node("branch").status == "skipped"
    assert result.trace_by_node("join").status == "success"
