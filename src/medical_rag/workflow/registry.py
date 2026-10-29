"""Registry for workflow node implementations and palette metadata."""

from copy import deepcopy

from .contracts import (
    CANONICAL_FIXED_TYPES,
    FIXED_NODE_CATEGORIES,
    Node,
    node_inputs,
    node_shared_producers,
    node_tags,
)


def port_info(spec) -> dict:
    """Wire form of an input port; the port name is already the mapping key."""
    return {
        "dtype": spec.dtype,
        "optional": spec.optional,
        "collect": spec.collect,
        "requires_tag": spec.requires_tag,
    }


class NodeRegistry:
    """Stores node implementations by their stable type identifier."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}

    def register(self, node: Node) -> None:
        if node.type in self._nodes:
            raise ValueError(f"node type already registered: {node.type}")
        self._nodes[node.type] = node

    def get(self, node_type: str) -> Node:
        return self._nodes[node_type]

    def metadata(self) -> list[dict]:
        return [
            {
                "type": node.type,
                "title": node.title,
                "category": node.category,
                "inputs": {name: port_info(spec) for name, spec in node_inputs(node).items()},
                "outputs": deepcopy(node.outputs),
                "tags": sorted(node_tags(node)),
                "shared_producers": [list(pair) for pair in node_shared_producers(node)],
                "config_schema": deepcopy(node.config_schema),
                "workflow_kinds": list(getattr(node, "workflow_kinds", ("workflow",))),
                "fixed": node.category in FIXED_NODE_CATEGORIES,
                "canonical": (CANONICAL_FIXED_TYPES.get(node.category) == node.type
                              or node.type in {"agent.input", "agent.output"}),
            }
            for node in sorted(self._nodes.values(), key=lambda registered: registered.type)
        ]
