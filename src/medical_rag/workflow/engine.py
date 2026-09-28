"""Deterministic asynchronous execution for workflow DAGs."""

from collections import defaultdict, deque
from dataclasses import dataclass
from time import perf_counter
from typing import Literal
from uuid import uuid4

from ..types import ApiError
from .contracts import ExecutionContext, NodeTrace
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

    def trace_by_node(self, node_id: str) -> NodeTrace:
        """Return a node's final trace entry or raise ``KeyError`` if absent."""
        for entry in self.trace:
            if entry.node_id == node_id:
                return entry
        raise KeyError(node_id)


class WorkflowEngine:
    """Runs registered workflow nodes in stable topological order."""

    def __init__(self, registry: NodeRegistry) -> None:
        self.registry = registry

    async def run(
        self, definition: WorkflowDefinition, inputs: dict[str, object]
    ) -> WorkflowRunResult:
        run_id = uuid4().hex
        context = ExecutionContext(
            run_id=run_id,
            workflow_id=definition.id,
            variables=dict(inputs),
        )
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
        skipped_generation_branch: set[str] = set()
        abstention_output: dict[str, object] | None = None
        output: dict[str, object] = {}

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

            node_inputs: dict[str, object] = {}
            if node.category == "input" and not incoming[node_id]:
                node_inputs.update(inputs)
            values_by_port: dict[str, list[object]] = defaultdict(list)
            missing_upstream_ports: list[str] = []
            for edge in incoming[node_id]:
                source_outputs = node_outputs.get(edge.source)
                if source_outputs is not None and edge.source_port in source_outputs:
                    values_by_port[edge.target_port].append(
                        source_outputs[edge.source_port]
                    )
                elif edge.target_port not in missing_upstream_ports:
                    missing_upstream_ports.append(edge.target_port)
            for port, values in values_by_port.items():
                node_inputs[port] = values[0] if len(values) == 1 else values

            if node_id in skipped_generation_branch:
                context.trace.append(
                    NodeTrace(
                        node_id=node_id,
                        status="skipped",
                        metadata={"reason": "gate_abstained"},
                    )
                )
                continue

            missing_inputs = [
                port
                for port in node.inputs
                if port not in node_inputs or port in missing_upstream_ports
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
            try:
                result = await node.execute(
                    node_inputs, definition_node.config, context
                )
            except ApiError as error:
                duration_ms = max(0, int((perf_counter() - started) * 1000))
                context.trace.append(
                    NodeTrace(
                        node_id=node_id,
                        status="failed",
                        duration_ms=duration_ms,
                        error={"code": error.code, "message": error.message},
                    )
                )
                return WorkflowRunResult(run_id, "failed", {}, context.trace)
            except Exception:
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
                    )
                )
                return WorkflowRunResult(run_id, "failed", {}, context.trace)

            # Explicitly skipped nodes may publish a harmless branch placeholder.
            # Engine-induced skips above never publish outputs.
            if result.status in ("success", "skipped") and result.outputs:
                node_outputs[node_id] = dict(result.outputs)
            context.trace.append(
                NodeTrace(
                    node_id=node_id,
                    status=result.status,
                    duration_ms=duration_ms,
                    error=self._safe_node_error(result.error) if result.error else None,
                )
            )
            if node.category == "output" and result.status == "success":
                output = dict(result.outputs)

            if result.status == "success" and result.outputs.get("abstained") is True:
                abstention_output = dict(result.outputs)
                generation_roots: set[str] = set()
                for candidate_id in self._descendants(node_id, outgoing):
                    try:
                        candidate = self.registry.get(nodes_by_id[candidate_id].type)
                    except KeyError:
                        continue
                    if candidate.category == "generation":
                        generation_roots.add(candidate_id)
                for generation_id in generation_roots:
                    skipped_generation_branch.update(
                        {generation_id, *self._descendants(generation_id, outgoing)}
                    )

        if abstention_output is not None:
            output = abstention_output
        return WorkflowRunResult(run_id, "success", output, context.trace)

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
