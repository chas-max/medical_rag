"""Runtime contracts shared by workflow nodes and the execution engine."""

from dataclasses import dataclass, field
from typing import Literal, Protocol

NodeTraceStatus = Literal["pending", "running", "success", "skipped", "failed"]
NodeResultStatus = Literal["success", "skipped", "failed"]


@dataclass(frozen=True)
class NodeTrace:
    node_id: str
    status: NodeTraceStatus
    duration_ms: int = 0
    metadata: dict[str, object] = field(default_factory=dict)
    error: dict[str, str] | None = None


@dataclass
class ExecutionContext:
    run_id: str
    workflow_id: str
    variables: dict[str, object]
    trace: list[NodeTrace] = field(default_factory=list)


@dataclass(frozen=True)
class NodeResult:
    outputs: dict[str, object]
    status: NodeResultStatus
    error: dict[str, str] | None = None


class Node(Protocol):
    type: str
    title: str
    category: str
    inputs: dict[str, str]
    outputs: dict[str, str]
    config_schema: dict[str, object]

    async def execute(
        self,
        inputs: dict[str, object],
        config: dict[str, object],
        context: ExecutionContext,
    ) -> NodeResult: ...
