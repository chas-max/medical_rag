"""Registry for workflow node implementations and palette metadata."""

from copy import deepcopy

from .contracts import Node


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
                "inputs": deepcopy(node.inputs),
                "outputs": deepcopy(node.outputs),
                "config_schema": deepcopy(node.config_schema),
            }
            for node in sorted(self._nodes.values(), key=lambda registered: registered.type)
        ]
