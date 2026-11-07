"""Runtime contracts shared by workflow nodes and the execution engine."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, Union

if TYPE_CHECKING:
    from ..dialogue_models import DialogueRun
    from ..short_term import ShortTerm

NodeTraceStatus = Literal["pending", "running", "success", "skipped", "inactive", "halt",
                          "failed"]
NodeResultStatus = Literal["success", "skipped", "failed", "halt"]

# 全局内置能力：由运行时决定，节点只读取，不再由工作流或单个节点配置。
CAPABILITY_WEB_SEARCH = "web_search"

# Per-run flag set by the rerank node when the cross-encoder is unavailable. The
# evidence gate reads it to fail closed even when no lexical candidate survived the
# node's own filter, and the citation node copies it into the public response.
RERANK_DEGRADED_VARIABLE = "_rerank_degraded"

# Per-run flag set by the drafting node when the model stopped at ``max_tokens``.
# The citation node reads it so a cut-off answer is reported as a truncation instead
# of being silently relabelled as "insufficient evidence". It is a run variable
# rather than a port because ``draft`` and ``evidence`` already carry the contract
# and existing published workflows must keep working unchanged.
MODEL_OUTPUT_TRUNCATED_VARIABLE = "_model_output_truncated"

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


@dataclass(frozen=True)
class PortSpec:
    """One input port: its dtype plus how many producers may connect to it.

    ``optional`` ports may stay unconnected, which is what lets a node be deleted
    from a graph without breaking its consumers. ``collect`` ports accept any
    number of edges and receive them as a list, so fan-in is a declared property
    of the port instead of an accident of how many edges happen to be wired.
    """

    name: str
    dtype: str
    optional: bool = False
    collect: bool = False
    requires_tag: str | None = None


DeclaredPort = Union[str, PortSpec]


def list_item(dtype: str) -> str | None:
    """Return ``T`` for a ``list<T>`` dtype, else ``None``."""
    if dtype.startswith("list<") and dtype.endswith(">"):
        return dtype[len("list<"):-1]
    return None


def port_accepts(source_dtype: str, target: PortSpec) -> bool:
    """Check one edge's types, allowing a collect port to swallow ``list<T>`` producers."""
    if source_dtype == target.dtype:
        return True
    return target.collect and list_item(source_dtype) == target.dtype


def port_spec(name: str, declared: DeclaredPort) -> PortSpec:
    """Read a bare dtype string as the shorthand for a required single-value port."""
    if isinstance(declared, PortSpec):
        return declared
    return PortSpec(name=name, dtype=declared)


def port_specs(ports: Mapping[str, DeclaredPort]) -> dict[str, PortSpec]:
    return {name: port_spec(name, declared) for name, declared in ports.items()}


def node_inputs(node: "Node") -> dict[str, PortSpec]:
    return port_specs(node.inputs)


def node_tags(node: "Node") -> frozenset[str]:
    """Capabilities a node's outputs vouch for, used by port ``requires_tag``."""
    return frozenset(getattr(node, "tags", ()))


def node_shared_producers(node: "Node") -> tuple[tuple[str, str], ...]:
    """Port pairs that must be fed by the same producer node."""
    return tuple(tuple(pair) for pair in getattr(node, "shared_producers", ()))


def supports(capabilities: dict[str, bool] | None, name: str) -> bool:
    """Report whether a global capability is active for the current run."""
    return bool((capabilities or {}).get(name))


@dataclass(frozen=True)
class Halt:
    """A handler's way of saying: publish these outputs and prune my subtree."""

    outputs: dict[str, object]


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
    node_id: str | None = None
    # Transient per-node diagnostics (for example ``degraded``). ``BuiltinNode`` copies
    # it into the node trace and clears it, so it never becomes a workflow port.
    node_metadata: dict[str, object] = field(default_factory=dict)
    # Explicit immutable per-run snapshot, never a workflow port or shared generator field.
    memory_context: str = ""
    # Same contract for the current conversation: recent turns plus a rolling digest.
    # Kept separate from memory_context so long-term recall and this session's own
    # words never get conflated in the prompt or in the trust boundary text.
    short_term: "ShortTerm | None" = None
    # Only the owning session's turn index may be searched, never a global corpus.
    conversation_id: str = ""
    # Request-scoped resolution slot owned by the caller. It is never a port and
    # never enters the public result, trace or SSE frames.
    dialogue: "DialogueRun | None" = None


@dataclass(frozen=True)
class NodeResult:
    outputs: dict[str, object]
    status: NodeResultStatus
    error: dict[str, str] | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class Node(Protocol):
    type: str
    title: str
    category: str
    inputs: dict[str, DeclaredPort]
    outputs: dict[str, str]
    config_schema: dict[str, object]

    async def execute(
        self,
        inputs: dict[str, object],
        config: dict[str, object],
        context: ExecutionContext,
    ) -> NodeResult: ...
