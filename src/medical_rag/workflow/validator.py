"""Validation helpers for persisted workflow definitions."""

from collections import defaultdict, deque
from dataclasses import dataclass

from .contracts import (
    Node,
    node_inputs,
    node_shared_producers,
    node_tags,
    port_accepts,
)
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
        for key, value in node.config.items():
            schema = registered.config_schema.get(key)
            if not isinstance(schema, dict):
                issues.append(ValidationIssue("INVALID_CONFIG", f"Unknown config field: {key}", node_id=node_id))
                continue
            expected = schema.get("type")
            valid_type = {
                "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
                "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
                "boolean": lambda item: isinstance(item, bool),
                "string": lambda item: isinstance(item, str),
            }.get(expected, lambda item: False)
            if (not valid_type(value)
                    or ("minimum" in schema and value < schema["minimum"])
                    or ("maximum" in schema and value > schema["maximum"])
                    or ("enum" in schema and value not in schema["enum"])):
                issues.append(ValidationIssue("INVALID_CONFIG", f"Invalid config field: {key}", node_id=node_id))
        specs = node_inputs(registered)
        if node.activation is not None and node.activation.port not in specs:
            issues.append(
                ValidationIssue(
                    "INVALID_ACTIVATION",
                    f"Activation port is not an input: {node.activation.port}",
                    node_id=node_id,
                )
            )
        if registered.category == "input":
            input_node_ids.append(node_id)
        if registered.category == "output":
            output_node_ids.append(node_id)

    # 固定节点约束：输入与输出类别各至少保留一个实例，画布侧的常驻与自动补回
    # 只是让这条规则更早暴露，发布/保存仍以这里为准。
    if not input_node_ids:
        issues.append(
            ValidationIssue("MISSING_INPUT_NODE", "工作流必须保留固定的输入节点")
        )
    if not output_node_ids:
        issues.append(
            ValidationIssue("MISSING_OUTPUT_NODE", "工作流必须保留固定的输出节点")
        )

    valid_inputs: set[tuple[str, str]] = set()
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in nodes_by_id}
    edges_by_port: dict[tuple[str, str], list[str]] = defaultdict(list)
    sources_by_port: dict[tuple[str, str], list[str]] = defaultdict(list)
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
            edges_by_port[(edge.target, edge.target_port)].append(edge.id)
            sources_by_port[(edge.target, edge.target_port)].append(edge.source)

        source_node = registered_nodes.get(edge.source)
        target_node = registered_nodes.get(edge.target)
        target_spec = node_inputs(target_node).get(edge.target_port) if target_node else None
        source_port_exists = (
            source_node is not None and edge.source_port in source_node.outputs
        )
        target_port_exists = target_spec is not None

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
            if not port_accepts(source_type, target_spec):
                issues.append(
                    ValidationIssue(
                        "PORT_TYPE_MISMATCH",
                        f"Port types differ: {source_type} != {target_spec.dtype}",
                        node_id=edge.target,
                        edge_id=edge.id,
                    )
                )
            else:
                valid_inputs.add((edge.target, edge.target_port))

    # A port that is not declared to collect has exactly one producer; taking the
    # last edge would otherwise depend on the order the graph JSON happens to list.
    for (target_id, port), edge_ids in edges_by_port.items():
        registered = registered_nodes.get(target_id)
        if registered is None or len(edge_ids) < 2:
            continue
        spec = node_inputs(registered).get(port)
        if spec is None or spec.collect:
            continue
        for edge_id in edge_ids[1:]:
            issues.append(
                ValidationIssue(
                    "MULTI_INPUT_EDGE",
                    f"Port takes one edge unless it collects: {port}",
                    node_id=target_id,
                    edge_id=edge_id,
                )
            )

    for node_id, registered in registered_nodes.items():
        for name, spec in node_inputs(registered).items():
            if spec.optional or (node_id, name) in valid_inputs:
                continue
            issues.append(
                ValidationIssue(
                    "MISSING_INPUT",
                    f"Required input is not connected: {name}",
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

    # 答案链的安全不变量改由节点声明携带，不再按具体类型名匹配：进入带 requires_tag
    # 端口的值，其生产者自身或到达它的全部路径上必须有背书该标签的节点。于是
    # 「生成必经证据门控、答案必经引用校验」仍然守得住，但重排、fusion、门控用哪个
    # 具体节点这类拓扑选择不再被写死在校验器里。
    dominance = _dominators(nodes_by_id, adjacency)
    for edge in definition.edges:
        registered = registered_nodes.get(edge.target)
        if registered is None or edge.source not in registered_nodes:
            continue
        spec = node_inputs(registered).get(edge.target_port)
        if spec is None or spec.requires_tag is None:
            continue
        if _covers_tag(edge.source, spec.requires_tag, dominance, registered_nodes):
            continue
        issues.append(
            ValidationIssue(
                "UNSAFE_ANSWER_FLOW",
                f"缺少 {spec.requires_tag} 环节，不能连接 {edge.target_port}",
                node_id=edge.target,
                edge_id=edge.id,
            )
        )

    # 生产者与校验者必须看见同一份证据，否则联网补充的网页块会绕过逐字校验。
    for node_id, registered in registered_nodes.items():
        for left, right in node_shared_producers(registered):
            left_sources = set(sources_by_port.get((node_id, left), ()))
            right_sources = set(sources_by_port.get((node_id, right), ()))
            if left_sources and right_sources and left_sources != right_sources:
                issues.append(
                    ValidationIssue(
                        "PROVENANCE_MISMATCH",
                        f"{left} 与 {right} 必须来自同一个节点",
                        node_id=node_id,
                    )
                )

    from .agentic_validation import agentic_issues
    issues.extend(agentic_issues(definition, registry))
    return issues


def _covers_tag(node_id: str, tag: str, dominance: dict[str, set[str]],
                registered_nodes: dict[str, Node]) -> bool:
    """True when this producer vouches for the tag, or every path into it does."""
    if tag in node_tags(registered_nodes[node_id]):
        return True
    ancestors = dominance.get(node_id, set()) - {node_id}
    return any(tag in node_tags(registered_nodes[ancestor])
               for ancestor in ancestors if ancestor in registered_nodes)


def _dominators(nodes_by_id: dict[str, WorkflowNode],
                adjacency: dict[str, list[str]]) -> dict[str, set[str]]:
    """Dominator sets for a DAG: ``dominators[n]`` sits on every path into ``n``.

    Roots are nodes with no incoming edge and only dominate themselves, which is
    what makes a bypassing path visible instead of silently satisfying a check.
    """
    predecessors: dict[str, set[str]] = defaultdict(set)
    for source, targets in adjacency.items():
        for target in targets:
            predecessors[target].add(source)

    remaining = {node_id: len(predecessors[node_id]) for node_id in nodes_by_id}
    successors = {node_id: set(targets) for node_id, targets in adjacency.items()}
    ready = deque(node_id for node_id in nodes_by_id if remaining[node_id] == 0)
    ordered: list[str] = []
    while ready:
        node_id = ready.popleft()
        ordered.append(node_id)
        for target in successors[node_id]:
            remaining[target] -= 1
            if remaining[target] == 0:
                ready.append(target)

    dominance: dict[str, set[str]] = {}
    for node_id in ordered:
        reached = [dominance[p] for p in predecessors[node_id] if p in dominance]
        shared = set.intersection(*reached) if reached else set()
        dominance[node_id] = shared | {node_id}
    return dominance
