import json
from dataclasses import dataclass, field

import pytest
from pydantic import ValidationError

from medical_rag.workflow.contracts import (
    ExecutionContext,
    Node,
    NodeResult,
    NodeTrace,
    PortSpec,
)
from medical_rag.workflow.definition import WorkflowDefinition, WorkflowNode
from medical_rag.workflow.registry import NodeRegistry


@dataclass
class StubNode:
    type: str
    title: str
    category: str = "input"
    inputs: dict[str, str | PortSpec] = field(default_factory=dict)
    outputs: dict[str, str] = field(
        default_factory=lambda: {"question": "string"}
    )
    config_schema: dict[str, object] = field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    shared_producers: tuple[tuple[str, str], ...] = ()

    async def execute(
        self,
        inputs: dict[str, object],
        config: dict[str, object],
        context: ExecutionContext,
    ) -> NodeResult:
        return NodeResult(outputs={}, status="success")


def test_registry_exposes_stable_node_metadata() -> None:
    registry = NodeRegistry()
    registry.register(StubNode("z.output", "输出", category="output"))
    registry.register(StubNode("query.input", "问题输入"))

    assert registry.metadata() == [
        {
            "type": "query.input",
            "title": "问题输入",
            "category": "input",
            "inputs": {},
            "outputs": {"question": "string"},
            "tags": [],
            "shared_producers": [],
            "config_schema": {},
            "fixed": True,
            "canonical": True,
        },
        {
            "type": "z.output",
            "title": "输出",
            "category": "output",
            "inputs": {},
            "outputs": {"question": "string"},
            "tags": [],
            "shared_producers": [],
            "config_schema": {},
            "fixed": True,
            "canonical": False,
        },
    ]


def test_registry_metadata_describes_port_cardinality_and_requirements() -> None:
    registry = NodeRegistry()
    registry.register(
        StubNode(
            "merge.collect",
            "汇聚",
            "transform",
            inputs={
                "items": PortSpec("items", "string", collect=True),
                "note": PortSpec("note", "string", optional=True),
            },
            outputs={"value": "string"},
            tags=frozenset({"answer.cited"}),
            shared_producers=(("draft", "evidence"),),
        )
    )

    inputs = registry.metadata()[0]["inputs"]

    assert inputs["items"] == {"dtype": "string", "optional": False,
                               "collect": True, "requires_tag": None}
    assert inputs["note"]["optional"] is True
    assert registry.metadata()[0]["tags"] == ["answer.cited"]
    assert registry.metadata()[0]["shared_producers"] == [["draft", "evidence"]]


def test_registry_rejects_duplicate_types_and_unknown_types() -> None:
    registry = NodeRegistry()
    node = StubNode("query.input", "问题输入")
    registry.register(node)

    with pytest.raises(ValueError, match="query.input"):
        registry.register(node)
    with pytest.raises(KeyError, match="missing"):
        registry.get("missing")


def test_stub_node_conforms_to_node_protocol() -> None:
    node: Node = StubNode("query.input", "问题输入")

    assert node.inputs == {}
    assert node.outputs == {"question": "string"}
    assert node.config_schema == {}


def test_registry_metadata_deep_copies_nested_node_metadata() -> None:
    node = StubNode(
        "query.input",
        "问题输入",
        inputs={"request": "object"},
        outputs={"question": "string"},
        config_schema={"nested": {"limits": [1, 2]}},
    )
    registry = NodeRegistry()
    registry.register(node)

    metadata = registry.metadata()
    metadata[0]["config_schema"]["nested"]["limits"].append(3)

    assert registry.metadata() == [
        {
            "type": "query.input",
            "title": "问题输入",
            "category": "input",
            "inputs": {"request": {"dtype": "object", "optional": False,
                                   "collect": False, "requires_tag": None}},
            "outputs": {"question": "string"},
            "tags": [],
            "shared_producers": [],
            "config_schema": {"nested": {"limits": [1, 2]}},
            "fixed": True,
            "canonical": True,
        }
    ]


def test_workflow_rejects_duplicate_node_ids_and_unknown_status() -> None:
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate(
            {
                "id": "demo",
                "name": "Demo",
                "version": 1,
                "status": "unknown",
                "nodes": [],
                "edges": [],
            }
        )

    with pytest.raises(ValidationError, match="duplicate node id"):
        WorkflowDefinition.model_validate(
            {
                "id": "demo",
                "name": "Demo",
                "version": 1,
                "status": "draft",
                "nodes": [
                    {"id": "same", "type": "a", "position": {"x": 1, "y": 2}},
                    {"id": "same", "type": "b", "position": {"x": 3, "y": 4}},
                ],
                "edges": [],
            }
        )


def test_workflow_rejects_duplicate_edge_ids_and_preserves_position_and_config() -> None:
    with pytest.raises(ValidationError, match="duplicate edge id"):
        WorkflowDefinition.model_validate(
            {
                "id": "demo",
                "name": "Demo",
                "version": 1,
                "status": "draft",
                "nodes": [],
                "edges": [
                    {"id": "edge", "source": "a", "source_port": "out", "target": "b", "target_port": "in"},
                    {"id": "edge", "source": "b", "source_port": "out", "target": "c", "target_port": "in"},
                ],
            }
        )

    definition = WorkflowDefinition.model_validate(
        {
            "id": "demo",
            "name": "Demo",
            "version": 1,
            "status": "draft",
            "nodes": [
                {
                    "id": "input",
                    "type": "query.input",
                    "position": {"x": 12.5, "y": -4},
                    "config": {"nested": [True, None, {"limit": 3}]},
                }
            ],
            "edges": [],
        }
    )
    assert definition.nodes[0].position == {"x": 12.5, "y": -4}
    assert definition.nodes[0].config == {"nested": [True, None, {"limit": 3}]}


def test_workflow_node_rejects_non_json_config_and_serializes_nested_values() -> None:
    with pytest.raises(ValidationError):
        WorkflowNode.model_validate(
            {"id": "input", "type": "query.input", "config": {"bad": object()}}
        )

    node = WorkflowNode.model_validate(
        {
            "id": "input",
            "type": "query.input",
            "config": {
                "text": "question",
                "enabled": True,
                "limit": 3,
                "ratio": 0.5,
                "nothing": None,
                "nested": [{"name": "value"}, [1, False]],
            },
        }
    )

    assert json.loads(node.model_dump_json())["config"] == {
        "text": "question",
        "enabled": True,
        "limit": 3,
        "ratio": 0.5,
        "nothing": None,
        "nested": [{"name": "value"}, [1, False]],
    }


def test_node_result_records_safe_status_and_outputs() -> None:
    result = NodeResult(outputs={"answer": "ok"}, status="success")
    assert result.status == "success"
    assert result.outputs["answer"] == "ok"


def test_runtime_context_and_trace_have_expected_defaults() -> None:
    trace = NodeTrace(node_id="input", status="pending")
    context = ExecutionContext(run_id="run-1", workflow_id="demo", variables={})

    assert trace.duration_ms == 0
    assert trace.metadata == {}
    assert trace.error is None
    assert context.trace == []
