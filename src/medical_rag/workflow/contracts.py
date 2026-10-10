"""Runtime contracts shared by workflow nodes and the execution engine."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

NodeTraceStatus = Literal["pending", "running", "success", "skipped", "failed"]
NodeResultStatus = Literal["success", "skipped", "failed"]

# 全局内置能力：由运行时决定，节点只读取，不再由工作流或单个节点配置。
CAPABILITY_WEB_SEARCH = "web_search"

# 固定节点：任何工作流都必须留下一条「进入」和一条「离开」路径，因此这两类节点
# 在画布上常驻、不可被随手删除。缺失时编辑器按 CANONICAL_FIXED_TYPES 补回。
FIXED_NODE_CATEGORIES = frozenset({"input", "output"})
CANONICAL_FIXED_TYPES: dict[str, str] = {
    "input": "query.input",
    "output": "answer.output",
}

# 一次流式事件：{"event": "claim" | "done" | "error", "data": ...}。节点只负责产出，
# 由 HTTP 层决定如何编码（当前是 SSE），因此引擎和节点不依赖任何传输格式。
StreamEvent = dict[str, object]
StreamEmitter = Callable[[StreamEvent], None]


def supports(capabilities: dict[str, bool] | None, name: str) -> bool:
    """Report whether a global capability is active for the current run."""
    return bool((capabilities or {}).get(name))


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
    capabilities: dict[str, bool] = field(default_factory=dict)
    trace: list[NodeTrace] = field(default_factory=list)
    # 只有流式运行才注入；为 None 时节点必须保持原来的整段生成行为。
    emit: StreamEmitter | None = None


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
