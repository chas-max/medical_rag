"""Validation helpers for persisted workflow definitions."""

from collections import deque
from dataclasses import dataclass

from .contracts import Node
from .definition import WorkflowDefinition, WorkflowNode
from .registry import NodeRegistry


@dataclass(frozen=True)
class ValidationIssue:
    """A stable, safe description of one workflow validation problem."""

    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None


def validate_workflow(
    definition: WorkflowDefinition, registry: NodeRegistry
) -> list[ValidationIssue]:
    """Return deterministic issues found in a workflow graph."""
    issues: list[ValidationIssue] = []
    node_ids: set[str] = set()
    nodes_by_id: dict[str, WorkflowNode] = {}

    for node in definition.nodes:
        if node.id in node_ids:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_NODE_ID",
                    f"Duplicate node id: {node.id}",
                    node_id=node.id,
                )
            )
        else:
            node_ids.add(node.id)
            nodes_by_id[node.id] = node

    edge_ids: set[str] = set()
    for edge in definition.edges:
        if edge.id in edge_ids:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_EDGE_ID",
                    f"Duplicate edge id: {edge.id}",
                    edge_id=edge.id,
                )
            )
        edge_ids.add(edge.id)

    registered_nodes: dict[str, Node] = {}
    input_node_ids: list[str] = []
    output_node_ids: list[str] = []
    for node_id, node in nodes_by_id.items():
        try:
            registered = registry.get(node.type)
        except KeyError:
            issues.append(
                ValidationIssue(
                    "UNKNOWN_NODE_TYPE",
                    f"Node type is not registered: {node.type}",
                    node_id=node_id,
                )
            )
            continue
        registered_nodes[node_id] = registered
        if registered.category == "input":
            input_node_ids.append(node_id)
        if registered.category == "output":
            output_node_ids.append(node_id)

    if not input_node_ids:
        issues.append(
            ValidationIssue("MISSING_INPUT_NODE", "Workflow needs an input node")
        )
    if not output_node_ids:
        issues.append(
            ValidationIssue("MISSING_OUTPUT_NODE", "Workflow needs an output node")
        )

    valid_inputs: set[tuple[str, str]] = set()
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in nodes_by_id}
    for edge in definition.edges:
        source_exists = edge.source in nodes_by_id
        target_exists = edge.target in nodes_by_id
        if not source_exists:
            issues.append(
                ValidationIssue(
                    "MISSING_SOURCE_NODE",
                    f"Source node does not exist: {edge.source}",
                    edge_id=edge.id,
                )
            )
        if not target_exists:
            issues.append(
                ValidationIssue(
                    "MISSING_TARGET_NODE",
                    f"Target node does not exist: {edge.target}",
                    edge_id=edge.id,
                )
            )
        if source_exists and target_exists:
            adjacency[edge.source].append(edge.target)

        source_node = registered_nodes.get(edge.source)
        target_node = registered_nodes.get(edge.target)
        source_port_exists = (
            source_node is not None and edge.source_port in source_node.outputs
        )
        target_port_exists = (
            target_node is not None and edge.target_port in target_node.inputs
        )

        if source_node is not None and not source_port_exists:
            issues.append(
                ValidationIssue(
                    "UNKNOWN_SOURCE_PORT",
                    f"Source port does not exist: {edge.source_port}",
                    node_id=edge.source,
                    edge_id=edge.id,
                )
            )
        if target_node is not None and not target_port_exists:
            issues.append(
                ValidationIssue(
                    "UNKNOWN_TARGET_PORT",
                    f"Target port does not exist: {edge.target_port}",
                    node_id=edge.target,
                    edge_id=edge.id,
                )
            )
        if source_port_exists and target_port_exists:
            source_type = source_node.outputs[edge.source_port]
            target_type = target_node.inputs[edge.target_port]
            if source_type != target_type:
                issues.append(
                    ValidationIssue(
                        "PORT_TYPE_MISMATCH",
                        f"Port types differ: {source_type} != {target_type}",
                        node_id=edge.target,
                        edge_id=edge.id,
                    )
                )
            else:
                valid_inputs.add((edge.target, edge.target_port))

    for node_id, registered in registered_nodes.items():
        for port in registered.inputs:
            if (node_id, port) not in valid_inputs:
                issues.append(
                    ValidationIssue(
                        "MISSING_INPUT",
                        f"Required input is not connected: {port}",
                        node_id=node_id,
                    )
                )

    reachable = set(input_node_ids)
    queue = deque(input_node_ids)
    while queue:
        node_id = queue.popleft()
        for target_id in adjacency[node_id]:
            if target_id not in reachable:
                reachable.add(target_id)
                queue.append(target_id)
    for node_id in nodes_by_id:
        if node_id not in reachable:
            issues.append(
                ValidationIssue(
                    "UNREACHABLE_NODE",
                    "Node cannot be reached from an input node",
                    node_id=node_id,
                )
            )

    in_degree = {node_id: 0 for node_id in nodes_by_id}
    for targets in adjacency.values():
        for target_id in targets:
            in_degree[target_id] += 1
    ready = deque(node_id for node_id in nodes_by_id if in_degree[node_id] == 0)
    ordered = 0
    while ready:
        node_id = ready.popleft()
        ordered += 1
        for target_id in adjacency[node_id]:
            in_degree[target_id] -= 1
            if in_degree[target_id] == 0:
                ready.append(target_id)
    if ordered != len(nodes_by_id):
        issues.append(ValidationIssue("CYCLE", "Workflow contains a cycle"))

    return issues
