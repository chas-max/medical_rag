"""Pydantic models for persisted workflow JSON definitions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

WorkflowStatus = Literal["draft", "validated", "published"]


class WorkflowNode(BaseModel):
    """A node instance placed in a workflow graph."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    position: dict[str, float] = Field(default_factory=dict)
    config: dict[str, object] = Field(default_factory=dict)


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
