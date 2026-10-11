"""Adapter from focused handlers to the public Node protocol."""

from collections.abc import Callable
from dataclasses import dataclass, field

from ..contracts import DeclaredPort, ExecutionContext, Halt, NodeResult


@dataclass
class BuiltinNode:
    type: str
    title: str
    category: str
    inputs: dict[str, DeclaredPort]
    outputs: dict[str, str]
    handler: Callable[[dict[str, object], dict[str, object], ExecutionContext],
                      dict[str, object] | Halt]
    config_schema: dict[str, object] = field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    shared_producers: tuple[tuple[str, str], ...] = ()

    async def execute(self, inputs: dict[str, object], config: dict[str, object],
                      context: ExecutionContext) -> NodeResult:
        produced = self.handler(inputs, config, context)
        if isinstance(produced, Halt):
            return NodeResult(produced.outputs, "halt")
        return NodeResult(produced, "success")
