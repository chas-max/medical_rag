from dataclasses import dataclass, field

from medical_rag.workflow.contracts import ExecutionContext, NodeResult
from medical_rag.workflow.definition import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)
from medical_rag.workflow.registry import NodeRegistry
from medical_rag.workflow.validator import validate_workflow


@dataclass
class StubNode:
    type: str
    title: str
    category: str
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    config_schema: dict[str, object] = field(default_factory=dict)

    async def execute(
        self,
        inputs: dict[str, object],
        config: dict[str, object],
        context: ExecutionContext,
    ) -> NodeResult:
        return NodeResult(outputs={}, status="success")


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


def make_registry(*nodes: StubNode) -> NodeRegistry:
    registry = NodeRegistry()
    for node in nodes:
        registry.register(node)
    return registry


def test_validator_returns_no_issues_for_a_connected_workflow() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "answer", "type": "answer.output"},
        ],
        [
            {
                "id": "question-edge",
                "source": "query",
                "source_port": "question",
                "target": "answer",
                "target_port": "question",
            }
        ],
    )
    registry = make_registry(
        StubNode(
            "query.input",
            "Query",
            "input",
            outputs={"question": "string"},
        ),
        StubNode(
            "answer.output",
            "Answer",
            "output",
            inputs={"question": "string"},
        ),
    )

    assert validate_workflow(definition, registry) == []


def test_validator_reports_cycle_and_missing_required_port() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "left", "type": "worker.left"},
            {"id": "right", "type": "worker.right"},
        ],
        [
            {
                "id": "query-left",
                "source": "query",
                "source_port": "question",
                "target": "left",
                "target_port": "question",
            },
            {
                "id": "left-right",
                "source": "left",
                "source_port": "value",
                "target": "right",
                "target_port": "left_value",
            },
            {
                "id": "right-left",
                "source": "right",
                "source_port": "value",
                "target": "left",
                "target_port": "context",
            },
        ],
    )
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode(
            "worker.left",
            "Left",
            "transform",
            inputs={"question": "string", "context": "string", "required": "string"},
            outputs={"value": "string"},
        ),
        StubNode(
            "worker.right",
            "Right",
            "output",
            inputs={"left_value": "string"},
            outputs={"value": "string"},
        ),
    )

    issues = validate_workflow(definition, registry)

    assert {issue.code for issue in issues} == {"CYCLE", "MISSING_INPUT"}
    missing = next(issue for issue in issues if issue.code == "MISSING_INPUT")
    assert (missing.node_id, missing.edge_id) == ("left", None)
    assert missing.message


def test_validator_reports_unknown_types_ports_and_incompatible_port_types() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "worker", "type": "worker.transform"},
            {"id": "answer", "type": "answer.output"},
            {"id": "missing", "type": "not.registered"},
        ],
        [
            {
                "id": "bad-source-port",
                "source": "query",
                "source_port": "unknown",
                "target": "worker",
                "target_port": "question",
            },
            {
                "id": "bad-target-port",
                "source": "worker",
                "source_port": "value",
                "target": "answer",
                "target_port": "unknown",
            },
            {
                "id": "wrong-type",
                "source": "worker",
                "source_port": "value",
                "target": "answer",
                "target_port": "evidence",
            },
        ],
    )
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode(
            "worker.transform",
            "Worker",
            "transform",
            inputs={"question": "string"},
            outputs={"value": "string"},
        ),
        StubNode(
            "answer.output",
            "Answer",
            "output",
            inputs={"evidence": "evidence"},
        ),
    )

    issues = validate_workflow(definition, registry)
    by_edge = {(issue.code, issue.edge_id) for issue in issues}

    assert ("UNKNOWN_NODE_TYPE", None) in by_edge
    assert ("UNKNOWN_SOURCE_PORT", "bad-source-port") in by_edge
    assert ("UNKNOWN_TARGET_PORT", "bad-target-port") in by_edge
    assert ("PORT_TYPE_MISMATCH", "wrong-type") in by_edge


def test_validator_reports_missing_edge_endpoints() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "answer", "type": "answer.output"},
        ],
        [
            {
                "id": "missing-source",
                "source": "ghost",
                "source_port": "question",
                "target": "answer",
                "target_port": "question",
            },
            {
                "id": "missing-target",
                "source": "query",
                "source_port": "question",
                "target": "ghost",
                "target_port": "question",
            },
        ],
    )
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode("answer.output", "Answer", "output", inputs={"question": "string"}),
    )

    issues = validate_workflow(definition, registry)

    assert {issue.code for issue in issues} >= {
        "MISSING_SOURCE_NODE",
        "MISSING_TARGET_NODE",
    }
    assert all(
        issue.edge_id
        for issue in issues
        if issue.code in {"MISSING_SOURCE_NODE", "MISSING_TARGET_NODE"}
    )


def test_validator_reports_missing_entry_exit_and_unreachable_nodes() -> None:
    definition = make_definition(
        [
            {"id": "first", "type": "worker.transform"},
            {"id": "second", "type": "worker.transform"},
        ],
        [],
    )
    registry = make_registry(
        StubNode("worker.transform", "Worker", "transform", outputs={"value": "string"})
    )

    issues = validate_workflow(definition, registry)

    assert {issue.code for issue in issues} >= {
        "MISSING_INPUT_NODE",
        "MISSING_OUTPUT_NODE",
        "UNREACHABLE_NODE",
    }
    assert {issue.node_id for issue in issues if issue.code == "UNREACHABLE_NODE"} == {
        "first",
        "second",
    }


def test_validator_reports_duplicate_node_and_edge_ids_stably() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "answer", "type": "answer.output"},
        ],
        [
            {
                "id": "same-edge",
                "source": "query",
                "source_port": "question",
                "target": "answer",
                "target_port": "question",
            }
        ],
    )
    definition.nodes.append(WorkflowNode(id="query", type="query.input"))
    definition.edges.append(
        WorkflowEdge(
            id="same-edge",
            source="query",
            source_port="question",
            target="answer",
            target_port="question",
        )
    )
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode("answer.output", "Answer", "output", inputs={"question": "string"}),
    )

    first = validate_workflow(definition, registry)
    second = validate_workflow(definition, registry)

    assert [
        (issue.code, issue.node_id, issue.edge_id, issue.message) for issue in first
    ] == [(issue.code, issue.node_id, issue.edge_id, issue.message) for issue in second]
    assert {issue.code for issue in first} >= {"DUPLICATE_NODE_ID", "DUPLICATE_EDGE_ID"}
