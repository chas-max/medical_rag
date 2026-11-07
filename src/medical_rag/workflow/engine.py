"""Deterministic asynchronous execution for workflow DAGs."""

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from time import monotonic, perf_counter
from typing import Literal
from uuid import uuid4

from ..public_errors import public_error
from ..types import ApiError, Evidence
from .agentic_runtime import AgentRuntime
from .contracts import (
    ExecutionContext,
    NodeTrace,
    PortSpec,
    StreamEmitter,
    list_item,
    node_inputs,
)
from .definition import WorkflowDefinition, WorkflowEdge, WorkflowNode
from .registry import NodeRegistry

RunStatus = Literal["success", "failed"]


@dataclass(frozen=True)
class WorkflowRunResult:
    """The final status, output, and ordered node trace for one workflow run."""

    run_id: str
    status: RunStatus
    output: dict[str, object]
    trace: list[NodeTrace]
    evidence: list[Evidence] = field(default_factory=list)

    def trace_by_node(self, node_id: str) -> NodeTrace:
        """Return a node's final trace entry or raise ``KeyError`` if absent."""
        for entry in self.trace:
            if entry.node_id == node_id:
                return entry
        raise KeyError(node_id)


class WorkflowEngine:
    """Runs registered workflow nodes in stable topological order."""

    def __init__(self, registry: NodeRegistry, capabilities: dict[str, bool] | None = None) -> None:
        self.registry = registry
        # 全局内置能力对所有工作流一视同仁：节点不再自行声明是否联网。
        self.capabilities = dict(capabilities or {})

    async def run(
        self, definition: WorkflowDefinition, inputs: dict[str, object],
        emit: StreamEmitter | None = None,
        memory_context: str = "",
        run_id: str | None = None,
        short_term: object | None = None,
        conversation_id: str = "",
        dialogue: object | None = None,
    ) -> WorkflowRunResult:
        run_id = run_id or uuid4().hex
        context = ExecutionContext(
            run_id=run_id,
            workflow_id=definition.id,
            variables=dict(inputs),
            capabilities=dict(self.capabilities),
            emit=emit,
            memory_context=memory_context,
            short_term=short_term,
            conversation_id=conversation_id,
            dialogue=dialogue,
        )
        runtime = context.variables.get("_agent_runtime")
        if isinstance(runtime, AgentRuntime):
            runtime.child_trace = context.trace
        else:
            runtime = None
            context.variables.pop("_agent_runtime", None)
        nodes_by_id: dict[str, WorkflowNode] = {}
        for node in definition.nodes:
            nodes_by_id.setdefault(node.id, node)
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in nodes_by_id}
        incoming: dict[str, list[WorkflowEdge]] = defaultdict(list)
        in_degree = {node_id: 0 for node_id in nodes_by_id}
        for edge in definition.edges:
            if edge.source not in nodes_by_id or edge.target not in nodes_by_id:
                continue
            outgoing[edge.source].append(edge.target)
            incoming[edge.target].append(edge)
            in_degree[edge.target] += 1

        ready = deque(node_id for node_id in nodes_by_id if in_degree[node_id] == 0)
        ordered_ids: list[str] = []
        while ready:
            node_id = ready.popleft()
            ordered_ids.append(node_id)
            for target_id in outgoing[node_id]:
                in_degree[target_id] -= 1
                if in_degree[target_id] == 0:
                    ready.append(target_id)

        if len(ordered_ids) != len(nodes_by_id):
            unresolved = next(
                node_id for node_id in nodes_by_id if in_degree[node_id] > 0
            )
            context.trace.append(
                NodeTrace(
                    node_id=unresolved,
                    status="failed",
                    error={"code": "CYCLE", "message": "Workflow contains a cycle"},
                )
            )
            return WorkflowRunResult(run_id, "failed", {}, context.trace)

        node_outputs: dict[str, dict[str, object]] = {}
        halted_subtree: set[str] = set()
        halt_output: dict[str, object] | None = None
        output: dict[str, object] = {}
        effective_evidence: list[Evidence] = []
        answer_evidence: dict[str, list[Evidence]] = {}
        final_evidence: list[Evidence] | None = None
        execution_order = {node_id: index for index, node_id in enumerate(ordered_ids)}

        for node_id in ordered_ids:
            definition_node = nodes_by_id[node_id]
            try:
                node = self.registry.get(definition_node.type)
            except KeyError:
                context.trace.append(
                    NodeTrace(
                        node_id=node_id,
                        status="failed",
                        error={
                            "code": "UNKNOWN_NODE_TYPE",
                            "message": "Node type is not registered",
                        },
                    )
                )
                return WorkflowRunResult(run_id, "failed", {}, context.trace)

            specs = node_inputs(node)
            resolved: dict[str, object] = {}
            if node.category == "input" and not incoming[node_id]:
                resolved.update(inputs)
            values_by_port: dict[str, list[object]] = defaultdict(list)
            for edge in sorted(incoming[node_id],
                               key=lambda connected: execution_order[connected.source]):
                source_outputs = node_outputs.get(edge.source)
                if source_outputs is None or edge.source_port not in source_outputs:
                    continue
                self._collect(values_by_port[edge.target_port], edge, source_outputs,
                              specs.get(edge.target_port), nodes_by_id)
            for port, collected in values_by_port.items():
                spec = specs.get(port)
                if spec is not None and spec.collect:
                    resolved[port] = collected
                else:
                    resolved[port] = collected[0]

            if node_id in halted_subtree:
                context.trace.append(
                    NodeTrace(
                        node_id=node_id,
                        status="skipped",
                        metadata={"reason": "halted_upstream"},
                    )
                )
                continue

            activation = definition_node.activation
            if activation is not None and not _matches(resolved, activation.port,
                                                       activation.equals):
                context.trace.append(
                    NodeTrace(
                        node_id=node_id,
                        status="inactive",
                        metadata={"reason": "activation_not_matched",
                                  "port": activation.port},
                    )
                )
                continue

            missing_inputs = [
                spec.name for spec in specs.values()
                if not spec.optional and spec.name not in resolved
            ]
            if missing_inputs:
                context.trace.append(
                    NodeTrace(
                        node_id=node_id,
                        status="skipped",
                        metadata={
                            "reason": "missing_required_input",
                            "ports": missing_inputs,
                        },
                    )
                )
                continue

            started = perf_counter()
            context.node_id = node_id
            try:
                result = await node.execute(
                    resolved, definition_node.config, context
                )
            except asyncio.CancelledError:
                context.trace.append(NodeTrace(
                    node_id=node_id, status="failed",
                    duration_ms=max(0, int((perf_counter() - started) * 1000)),
                    error={"code": "TIMEOUT" if runtime is not None and monotonic() >= runtime.deadline else "CANCELLED",
                           "message": "节点等待已停止"}))
                raise
            except TimeoutError:
                context.trace.append(NodeTrace(
                    node_id=node_id, status="failed",
                    duration_ms=max(0, int((perf_counter() - started) * 1000)),
                    error={"code": "TIMEOUT" if runtime is not None else "NODE_EXECUTION_ERROR",
                           "message": "节点等待已停止" if runtime is not None else "节点执行失败"}))
                return WorkflowRunResult(run_id, "failed", {}, context.trace, effective_evidence)
            except ApiError as error:
                duration_ms = max(0, int((perf_counter() - started) * 1000))
                context.trace.append(
                    NodeTrace(
                        node_id=node_id,
                        status="failed",
                        duration_ms=duration_ms,
                        error=public_error(error.code).data(),
                    )
                )
                return WorkflowRunResult(run_id, "failed", {}, context.trace)
            except Exception:  # noqa: BLE001 - never expose a node's internal failure
                duration_ms = max(0, int((perf_counter() - started) * 1000))
                context.trace.append(
                    NodeTrace(
                        node_id=node_id,
                        status="failed",
                        duration_ms=duration_ms,
                        error={
                            "code": "NODE_EXECUTION_ERROR",
                            "message": "节点执行失败",
                        },
                    )
                )
                return WorkflowRunResult(run_id, "failed", {}, context.trace)

            duration_ms = max(0, int((perf_counter() - started) * 1000))
            if result.status == "failed":
                context.trace.append(
                    NodeTrace(
                        node_id=node_id,
                        status="failed",
                        duration_ms=duration_ms,
                        error=self._safe_node_error(result.error),
                        metadata={},
                    )
                )
                return WorkflowRunResult(run_id, "failed", {}, context.trace)

            # skipped 表示"本次没有值"：只有 success 与 halt 才向下游发布输出，
            # 消费者用 optional/collect 端口吸收缺口，而不是靠占位对象。
            if result.status in ("success", "halt") and result.outputs:
                node_outputs[node_id] = dict(result.outputs)
                for port, dtype in node.outputs.items():
                    value = result.outputs.get(port)
                    if (dtype == "evidence_list" and isinstance(value, list)
                            and all(isinstance(item, Evidence) for item in value)):
                        effective_evidence = list(value)
                        if runtime is not None and node.type in {"retrieval.dense", "retrieval.bm25", "retrieval.multi"}:
                            runtime.retrieved.extend(value)
                if "answer" in result.outputs:
                    supplied = resolved.get("evidence")
                    if isinstance(supplied, list) and all(isinstance(item, Evidence) for item in supplied):
                        answer_evidence[node_id] = list(supplied)
                    else:
                        for edge in incoming[node_id]:
                            if edge.source in answer_evidence and edge.target_port == "answer":
                                answer_evidence[node_id] = answer_evidence[edge.source]
            context.trace.append(
                NodeTrace(
                    node_id=node_id,
                    status=result.status,
                    duration_ms=duration_ms,
                    error=self._safe_node_error(result.error) if result.error else None,
                    metadata=dict(result.metadata),
                )
            )
            if node.category == "output" and result.status == "success":
                output = dict(result.outputs)
                if node_id in answer_evidence:
                    final_evidence = answer_evidence[node_id]
                elif (isinstance(result.outputs.get("evidence"), list)
                      and all(isinstance(item, Evidence) for item in result.outputs["evidence"])):
                    final_evidence = list(result.outputs["evidence"])

            if result.status == "halt":
                halt_output = dict(result.outputs)
                halted_subtree.update(self._descendants(node_id, outgoing))

        if halt_output is not None:
            output = halt_output
        return WorkflowRunResult(run_id, "success", output, context.trace,
                                 effective_evidence if final_evidence is None else final_evidence)

    def _collect(self, bucket: list[object], edge: WorkflowEdge,
                 source_outputs: dict[str, object], spec: PortSpec | None,
                 nodes_by_id: dict[str, WorkflowNode]) -> None:
        """Add one edge's value to a port bucket, honouring the port's cardinality."""
        value = source_outputs[edge.source_port]
        if spec is None or not spec.collect:
            bucket.append(value)
            return
        source_dtype = self.registry.get(nodes_by_id[edge.source].type).outputs.get(
            edge.source_port
        )
        # A collect port of dtype T accepts T edges and list<T> edges; the latter
        # contribute their items, which is how one fan-out producer feeds a join.
        if source_dtype is not None and list_item(source_dtype) == spec.dtype:
            bucket.extend(value if isinstance(value, list) else [value])
        else:
            bucket.append(value)

    @staticmethod
    def _descendants(node_id: str, outgoing: dict[str, list[str]]) -> set[str]:
        descendants: set[str] = set()
        queue = deque(outgoing.get(node_id, []))
        while queue:
            candidate_id = queue.popleft()
            if candidate_id in descendants:
                continue
            descendants.add(candidate_id)
            queue.extend(outgoing.get(candidate_id, []))
        return descendants

    @staticmethod
    def _safe_node_error(_error: dict[str, str] | None) -> dict[str, str]:
        """Return generic trace data instead of exposing node-supplied strings."""
        return {"code": "NODE_EXECUTION_ERROR", "message": "节点执行失败"}


def _matches(resolved: dict[str, object], port: str, expected: object) -> bool:
    """Evaluate a node's activation condition against its resolved inputs."""
    if port not in resolved:
        return False
    actual = resolved[port]
    if isinstance(actual, list):
        return expected in actual
    return actual == expected
