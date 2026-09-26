"""Adapter from focused handlers to the public Node protocol."""

from collections.abc import Callable
from dataclasses import dataclass, field

from ..contracts import ExecutionContext, NodeResult


@dataclass
class BuiltinNode:
    type: str
    title: str
    category: str
    inputs: dict[str, str]
    outputs: dict[str, str]
    handler: Callable[[dict[str, object], dict[str, object], ExecutionContext], dict[str, object]]
    config_schema: dict[str, object] = field(default_factory=dict)

    async def execute(self, inputs: dict[str, object], config: dict[str, object],
                      context: ExecutionContext) -> NodeResult:
        return NodeResult(self.handler(inputs, config, context), "success")
