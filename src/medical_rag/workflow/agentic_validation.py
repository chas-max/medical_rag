"""Kind, pinned-reference and evidence-provenance checks for iteration DAGs."""

from math import isfinite
from typing import TYPE_CHECKING, Any

from .definition import WorkflowDefinition
from .registry import NodeRegistry
from .repository import WorkflowRepository

if TYPE_CHECKING:
    from .validator import ValidationIssue


def config_valid(config: dict[str, Any], schemas: dict[str, Any]) -> bool:
    """Require a pinned version and validate the scalar controller budget."""
    if "workflow_version" not in config:
        return False
    for name, value in config.items():
        schema = schemas.get(name)
        if schema is None:
            return False
        valid = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }
        if not valid.get(schema["type"], False):
            return False
        if schema["type"] in {"integer", "number"} and not isfinite(value):
            return False
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    return True


def reference_child(
    config: dict[str, Any], repository: WorkflowRepository | None
) -> WorkflowDefinition | None:
    """Broken persisted definitions are invalid references, never fallbacks."""
    if repository is None or "workflow_version" not in config:
        return None
    try:
        child = repository.get(
            config.get("workflow_id", "local-retrieval-correction"),
            config["workflow_version"],
        )
    except (KeyError, ValueError, TypeError):
        return None
    return (
        child
        if child.kind == "agent_iteration" and child.status == "published"
        else None
    )


def agentic_issues(
    definition: WorkflowDefinition, registry: NodeRegistry
) -> list["ValidationIssue"]:
    """Supplement general DAG checks without loosening any ordinary invariants."""
    from .validator import ValidationIssue, validate_workflow

    issues = []
    registered = {}
    for node in definition.nodes:
        try:
            registered[node.id] = registry.get(node.type)
        except KeyError:
            continue
        if definition.kind not in getattr(
            registered[node.id], "workflow_kinds", ("workflow",)
        ):
            issues.append(
                ValidationIssue(
                    "INVALID_NODE_KIND", "节点不适用于此工作流用途", node_id=node.id
                )
            )
        if node.type == "retrieval.agentic":
            if definition.kind == "agent_iteration":
                continue  # No recursion: nesting is forbidden regardless of the reference.
            child = reference_child(node.config, registered[node.id].repository)
            if (
                not config_valid(node.config, registered[node.id].config_schema)
                or child is None
            ):
                issues.append(
                    ValidationIssue(
                        "INVALID_AGENT_REFERENCE",
                        "须引用已发布的固定版本迭代子图",
                        node_id=node.id,
                    )
                )
            else:
                issues.extend(validate_workflow(child, registry))
    if definition.kind != "agent_iteration":
        return issues
    for boundary in ("agent.input", "agent.output"):
        matches = [node for node in definition.nodes if node.type == boundary]
        if len(matches) != 1 or matches[0].activation is not None:
            issues.append(
                ValidationIssue(
                    "INVALID_AGENT_BOUNDARY", "迭代子图须有唯一且无条件的输入与输出"
                )
            )
    for operation in ("evidence.assess", "question.refine"):
        if sum(node.type == operation for node in definition.nodes) != 1:
            issues.append(
                ValidationIssue("INVALID_AGENT_OPERATION", "须有唯一的评估与规划操作")
            )
    if any(
        node.type != "agent.input"
        and getattr(registered.get(node.id), "category", None) == "input"
        or node.type != "agent.output"
        and getattr(registered.get(node.id), "category", None) == "output"
        for node in definition.nodes
    ):
        issues.append(ValidationIssue("INVALID_AGENT_BOUNDARY", "迭代子图边界类型错误"))

    # Evidence-producing model nodes cannot become sources even if their declared
    # output happens to be evidence_list. Trust only explicit retrieval adapters.
    trusted_sources = {
        "retrieval.dense",
        "retrieval.bm25",
        "retrieval.multi",
        "agent.input",
    }
    transforms = {
        "retrieval.rrf",
        "retrieval.rerank",
        "evidence.accumulate",
        "agent.output",
    }
    incoming = {}
    for edge in definition.edges:
        source = registered.get(edge.source)
        if (
            source is not None
            and source.outputs.get(edge.source_port) == "evidence_list"
        ):
            incoming.setdefault(edge.target, []).append(edge.source)

    def safe(node_id: str, path: set[str]) -> bool:
        node = registered.get(node_id)
        if node is None or node_id in path:
            return False
        if node.type in trusted_sources:
            return True
        if node.type not in transforms or not incoming.get(node_id):
            return False
        return all(safe(source, path | {node_id}) for source in incoming[node_id])

    for node in definition.nodes:
        if node.type == "agent.output" and not safe(node.id, set()):
            issues.append(
                ValidationIssue(
                    "UNSAFE_AGENT_EVIDENCE",
                    "输出证据必须来自当前输入或真实检索",
                    node_id=node.id,
                )
            )
    return issues
