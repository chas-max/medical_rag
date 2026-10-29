import re
from dataclasses import dataclass, field
from typing import Callable

import pytest

from medical_rag.types import ApiError
from medical_rag.workflow.contracts import (
    ExecutionContext,
    NodeResult,
    PortSpec,
)
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
    inputs: dict[str, str | PortSpec] = field(default_factory=dict)
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
async def test_engine_prunes_downstream_when_a_node_halts() -> None:
    order: list[str] = []

    def result_for(name: str, outputs: dict[str, object], status: str = "success") -> NodeFunction:
        def execute(inputs: dict[str, object], context: ExecutionContext) -> NodeResult:
            order.append(name)
            return NodeResult(outputs=outputs, status=status)

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
            function=result_for("gate", {"answer": "资料不足", "abstained": True}, "halt"),
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
            inputs={"values": PortSpec("values", "string", collect=True)},
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
async def test_collect_port_receives_whatever_its_producers_published() -> None:
    """A collect join runs on the branches that produced values; absence is not a stall."""
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
            function=lambda inputs, context: NodeResult(outputs={}, status="skipped"),
        ),
        FunctionalNode(
            "join.node",
            "Join",
            "output",
            inputs={"values": PortSpec("values", "string", collect=True)},
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

    assert called == ["join"]
    assert result.output == {"values": ["one"]}


@pytest.mark.asyncio
async def test_skipped_node_publishes_nothing_and_optional_ports_absorb_the_gap() -> None:
    """skipped 不再发布占位输出：缺口由 optional/collect 端口吸收。"""
    registry = make_registry(
        FunctionalNode(
            "query.input",
            "Input",
            "input",
            outputs={"question": "string"},
            function=lambda inputs, context: NodeResult({"question": "q"}, "success"),
        ),
        FunctionalNode(
            "question.hyde",
            "Hyde",
            "question",
            inputs={"question": "string"},
            outputs={"query": "string"},
            function=lambda inputs, context: NodeResult({"query": "不该出现在下游"}, "skipped"),
        ),
        FunctionalNode(
            "merge.collect",
            "Merge",
            "question",
            inputs={"question": "string",
                    "items": PortSpec("items", "string", optional=True, collect=True)},
            outputs={"values": "list<string>"},
            function=lambda inputs, context: NodeResult(
                {"values": [inputs["question"], *inputs.get("items", [])]}, "success"
            ),
        ),
        FunctionalNode(
            "search.output",
            "Search",
            "output",
            inputs={"values": "list<string>"},
            outputs={"values": "list<string>"},
        ),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "hyde", "type": "question.hyde"},
            {"id": "merge", "type": "merge.collect"},
            {"id": "output", "type": "search.output"},
        ],
        [
            {
                "id": "e1",
                "source": "query",
                "source_port": "question",
                "target": "hyde",
                "target_port": "question",
            },
            {
                "id": "e2",
                "source": "hyde",
                "source_port": "query",
                "target": "merge",
                "target_port": "items",
            },
            {
                "id": "e3",
                "source": "query",
                "source_port": "question",
                "target": "merge",
                "target_port": "question",
            },
            {
                "id": "e4",
                "source": "merge",
                "source_port": "values",
                "target": "output",
                "target_port": "values",
            },
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "q"})

    assert result.status == "success"
    assert result.trace_by_node("hyde").status == "skipped"
    assert result.output == {"values": ["q"]}


@pytest.mark.asyncio
async def test_activation_decides_the_branch_and_inactive_nodes_do_not_run() -> None:
    """路由条件写在图上：不匹配的节点既不执行，也不向下游发布任何值。"""
    order: list[str] = []

    def record(name: str, outputs: dict[str, object]) -> NodeFunction:
        def execute(inputs: dict[str, object], context: ExecutionContext) -> NodeResult:
            order.append(name)
            return NodeResult(outputs=outputs, status="success")

        return execute

    registry = make_registry(
        FunctionalNode("query.input", "Query", "input", outputs={"question": "string"},
                       function=record("query", {"question": "q"})),
        FunctionalNode("question.router", "Router", "question", inputs={"question": "string"},
                       outputs={"route": "selector"}, function=record("router", {"route": "rewrite"})),
        FunctionalNode("question.rewrite", "Rewrite", "question",
                       inputs={"question": "string",
                               "route": PortSpec("route", "selector", optional=True)},
                       outputs={"value": "string"}, function=record("rewrite", {"value": "改写问题"})),
        FunctionalNode("question.hyde", "Hyde", "question",
                       inputs={"question": "string",
                               "route": PortSpec("route", "selector", optional=True)},
                       outputs={"value": "string"}, function=record("hyde", {"value": "模拟答案"})),
        FunctionalNode("merge.first_active", "Merge", "question",
                       inputs={"question": "string",
                               "candidates": PortSpec("candidates", "string", optional=True,
                                                      collect=True)},
                       outputs={"query": "string"},
                       function=lambda inputs, context: NodeResult(
                           outputs={"query": (inputs.get("candidates") or [inputs["question"]])[0]},
                           status="success")),
        FunctionalNode("search.output", "Search", "output", inputs={"query": "string"},
                       outputs={"query": "string"}),
    )
    definition = WorkflowDefinition(
        id="demo", name="Demo", version=1, status="draft",
        nodes=[
            WorkflowNode(id="query", type="query.input"),
            WorkflowNode(id="router", type="question.router"),
            WorkflowNode(id="rewrite", type="question.rewrite",
                         activation={"port": "route", "equals": "rewrite"}),
            WorkflowNode(id="hyde", type="question.hyde",
                         activation={"port": "route", "equals": "hyde"}),
            WorkflowNode(id="merge", type="merge.first_active"),
            WorkflowNode(id="output", type="search.output"),
        ],
        edges=[
            WorkflowEdge(id="q-r", source="query", source_port="question",
                         target="router", target_port="question"),
            WorkflowEdge(id="q-w", source="query", source_port="question",
                         target="rewrite", target_port="question"),
            WorkflowEdge(id="q-h", source="query", source_port="question",
                         target="hyde", target_port="question"),
            WorkflowEdge(id="r-w", source="router", source_port="route",
                         target="rewrite", target_port="route"),
            WorkflowEdge(id="r-h", source="router", source_port="route",
                         target="hyde", target_port="route"),
            WorkflowEdge(id="q-m", source="query", source_port="question",
                         target="merge", target_port="question"),
            WorkflowEdge(id="w-m", source="rewrite", source_port="value",
                         target="merge", target_port="candidates"),
            WorkflowEdge(id="h-m", source="hyde", source_port="value",
                         target="merge", target_port="candidates"),
            WorkflowEdge(id="m-o", source="merge", source_port="query",
                         target="output", target_port="query"),
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "q"})

    assert result.status == "success"
    assert order == ["query", "router", "rewrite"]
    assert result.trace_by_node("hyde").status == "inactive"
    assert result.trace_by_node("rewrite").status == "success"
    # 只有被选中的分支向汇聚端口贡献了一个值，未被选中的分支什么都不会发布。
    assert result.output == {"query": "改写问题"}


@pytest.mark.asyncio
async def test_unconnected_optional_port_still_runs_the_node() -> None:
    registry = make_registry(
        FunctionalNode("query.input", "Query", "input", outputs={"question": "string"}),
        FunctionalNode(
            "question.rewrite", "Rewrite", "question",
            inputs={"question": "string", "route": PortSpec("route", "selector", optional=True)},
            outputs={"value": "string"},
            function=lambda inputs, context: NodeResult(
                outputs={"value": f"{inputs['question']}:{inputs.get('route')}"}, status="success"),
        ),
        FunctionalNode("search.output", "Search", "output", inputs={"value": "string"},
                       outputs={"value": "string"}),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "rewrite", "type": "question.rewrite"},
            {"id": "output", "type": "search.output"},
        ],
        [
            {"id": "e1", "source": "query", "source_port": "question",
             "target": "rewrite", "target_port": "question"},
            {"id": "e2", "source": "rewrite", "source_port": "value",
             "target": "output", "target_port": "value"},
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "q"})

    assert result.status == "success"
    assert result.output == {"value": "q:None"}


@pytest.mark.asyncio
async def test_collect_port_flattens_a_list_producer_into_items() -> None:
    registry = make_registry(
        FunctionalNode("query.input", "Query", "input", outputs={"question": "string"}),
        FunctionalNode("question.decompose", "Decompose", "question", inputs={"question": "string"},
                       outputs={"values": "list<string>"},
                       function=lambda inputs, context: NodeResult(
                           outputs={"values": ["a", "b"]}, status="success")),
        FunctionalNode("merge.collect", "Merge", "question",
                       inputs={"items": PortSpec("items", "string", collect=True)},
                       outputs={"value": "string"},
                       function=lambda inputs, context: NodeResult(
                           outputs={"value": "|".join(inputs["items"])}, status="success")),
        FunctionalNode("search.output", "Search", "output", inputs={"value": "string"},
                       outputs={"value": "string"}),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "decompose", "type": "question.decompose"},
            {"id": "merge", "type": "merge.collect"},
            {"id": "output", "type": "search.output"},
        ],
        [
            {"id": "e1", "source": "query", "source_port": "question",
             "target": "decompose", "target_port": "question"},
            {"id": "e2", "source": "decompose", "source_port": "values",
             "target": "merge", "target_port": "items"},
            {"id": "e3", "source": "merge", "source_port": "value",
             "target": "output", "target_port": "value"},
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "q"})

    assert result.output == {"value": "a|b"}


@pytest.mark.asyncio
async def test_collect_port_orders_values_by_execution_not_edge_order() -> None:
    registry = make_registry(
        FunctionalNode("query.input", "Query", "input", outputs={"question": "string"},
                       function=lambda inputs, context: NodeResult(
                           outputs={"question": "q"}, status="success")),
        FunctionalNode("branch.first", "First", "transform", inputs={"question": "string"},
                       outputs={"value": "string"},
                       function=lambda inputs, context: NodeResult(outputs={"value": "first"},
                                                                   status="success")),
        FunctionalNode("branch.second", "Second", "transform", inputs={"prior": "string"},
                       outputs={"value": "string"},
                       function=lambda inputs, context: NodeResult(outputs={"value": "second"},
                                                                   status="success")),
        FunctionalNode("join.node", "Join", "output",
                       inputs={"values": PortSpec("values", "string", collect=True)}),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "second", "type": "branch.second"},
            {"id": "first", "type": "branch.first"},
            {"id": "join", "type": "join.node"},
        ],
        # second 排在 first 之后执行，但它的连线写在 JSON 前面；
        # 汇聚端口按执行次序收值，而不是按连线的书写次序。
        [
            {"id": "e1", "source": "query", "source_port": "question",
             "target": "first", "target_port": "question"},
            {"id": "f-s", "source": "first", "source_port": "value",
             "target": "second", "target_port": "prior"},
            {"id": "s-j", "source": "second", "source_port": "value",
             "target": "join", "target_port": "values"},
            {"id": "f-j", "source": "first", "source_port": "value",
             "target": "join", "target_port": "values"},
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "q"})

    assert result.output == {"values": ["first", "second"]}


@pytest.mark.asyncio
async def test_halt_prunes_only_the_halting_nodes_downstream() -> None:
    registry = make_registry(
        FunctionalNode("query.input", "Query", "input", outputs={"question": "string"}),
        FunctionalNode("evidence.gate", "Gate", "evidence", inputs={"question": "string"},
                       outputs={"answer": "string"},
                       function=lambda inputs, context: NodeResult(
                           outputs={"answer": "现有资料不足"}, status="halt")),
        FunctionalNode("answer.output", "Output", "output", inputs={"answer": "string"},
                       outputs={"answer": "string"}),
        FunctionalNode("audit.record", "Audit", "audit", inputs={"question": "string"},
                       outputs={"recorded": "boolean"},
                       function=lambda inputs, context: NodeResult(
                           outputs={"recorded": True}, status="success")),
    )
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "gate", "type": "evidence.gate"},
            {"id": "output", "type": "answer.output"},
            {"id": "audit", "type": "audit.record"},
        ],
        [
            {"id": "q-g", "source": "query", "source_port": "question",
             "target": "gate", "target_port": "question"},
            {"id": "g-o", "source": "gate", "source_port": "answer",
             "target": "output", "target_port": "answer"},
            {"id": "q-a", "source": "query", "source_port": "question",
             "target": "audit", "target_port": "question"},
        ],
    )

    result = await WorkflowEngine(registry).run(definition, {"question": "q"})

    assert result.status == "success"
    assert result.output == {"answer": "现有资料不足"}
    assert result.trace_by_node("gate").status == "halt"
    assert result.trace_by_node("output").status == "skipped"
    assert result.trace_by_node("audit").status == "success"
