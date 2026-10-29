"""Derive the run inputs a workflow consumes, from the graph itself."""

from .definition import WorkflowDefinition
from .registry import NodeRegistry


def run_inputs_schema(definition: WorkflowDefinition, registry: NodeRegistry) -> list[dict]:
    """Every output port of an input node is a run parameter.

    端口被下游连线用掉才算必填；没被用到的端口留着也不会挡住运行。这样加一个
    输入节点、或把某个入参整个删掉，都不需要再改服务层的校验代码。
    """
    consumed: dict[str, set[str]] = {}
    for edge in definition.edges:
        consumed.setdefault(edge.source, set()).add(edge.source_port)

    schema: list[dict] = []
    for node in definition.nodes:
        try:
            registered = registry.get(node.type)
        except KeyError:
            continue
        if registered.category != "input":
            continue
        for name, dtype in registered.outputs.items():
            schema.append({
                "node_id": node.id,
                "name": name,
                "dtype": dtype,
                "required": name in consumed.get(node.id, set()),
            })
    return sorted(schema, key=lambda item: (item["node_id"], item["name"]))
