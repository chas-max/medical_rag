"""Pydantic models for persisted workflow JSON definitions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

WorkflowStatus = Literal["draft", "validated", "published"]


class NodeActivation(BaseModel):
    """Runs a node only when one of its inputs carries the expected value.

    Declaring the condition on the instance keeps branch selection out of the
    node implementation, so a router is just another deletable node.
    """

    model_config = ConfigDict(extra="forbid")

    port: str
    equals: JsonValue


class WorkflowNode(BaseModel):
    """A node instance placed in a workflow graph."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    position: dict[str, float] = Field(default_factory=dict)
    config: dict[str, JsonValue] = Field(default_factory=dict)
    activation: NodeActivation | None = None


class WorkflowEdge(BaseModel):
    """A directed connection between two named node ports."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    source_port: str
    target: str
    target_port: str


class WorkflowDefinition(BaseModel):
    """A versioned, JSON-serializable workflow graph."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    version: int
    status: WorkflowStatus
    kind: Literal["workflow", "agent_iteration"] = "workflow"
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "WorkflowDefinition":
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("duplicate node id")

        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate edge id")
        return self


class WorkflowRunRequest(BaseModel):
    """Inputs supplied when starting a workflow run."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, object] = Field(default_factory=dict)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    memory_consent: bool = Field(default=False, strict=True)
