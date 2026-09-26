from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from medical_rag.workflow.contracts import (
    ExecutionContext,
    NodeResult,
    NodeTrace,
)
from medical_rag.workflow.definition import WorkflowDefinition
from medical_rag.workflow.registry import NodeRegistry


@dataclass
class StubNode:
    type: str
    title: str
    category: str = "input"
    inputs: dict[str, str] = None
    outputs: dict[str, str] = None
    config_schema: dict[str, object] = None

    def __post_init__(self) -> None:
        self.inputs = {} if self.inputs is None else self.inputs
        self.outputs = {"question": "string"} if self.outputs is None else self.outputs
        self.config_schema = {} if self.config_schema is None else self.config_schema


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
            "config_schema": {},
        },
        {
            "type": "z.output",
            "title": "输出",
            "category": "output",
            "inputs": {},
            "outputs": {"question": "string"},
            "config_schema": {},
        },
    ]


def test_registry_rejects_duplicate_types_and_unknown_types() -> None:
    registry = NodeRegistry()
    node = StubNode("query.input", "问题输入")
    registry.register(node)

    with pytest.raises(ValueError, match="query.input"):
        registry.register(node)
    with pytest.raises(KeyError, match="missing"):
        registry.get("missing")


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
