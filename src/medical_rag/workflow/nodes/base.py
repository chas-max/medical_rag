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
    workflow_kinds: tuple[str, ...] = ("workflow",)
    worker_lock: object = None

    async def execute(self, inputs: dict[str, object], config: dict[str, object],
                      context: ExecutionContext) -> NodeResult:
        runtime = context.variables.get("_agent_runtime")
        context.node_metadata.clear()
        try:
            if runtime is not None:
                from ..agentic_runtime import bounded_sync
                inputs = dict(inputs)
                if self.type == "retrieval.rerank":
                    if not runtime.queries:
                        return NodeResult({}, "skipped")
                    inputs["question"] = runtime.question
                if self.category == "retrieval":
                    if not runtime.queries or not runtime.allow_repair:
                        return NodeResult({}, "skipped")
                    runtime.progress("retrieving")
                    if "queries" in inputs:
                        inputs["queries"] = runtime.filter_queries(inputs["queries"], record=False)
                        inputs["queries"] = runtime.reserve_queries(inputs["queries"], config.get("strategy", "hybrid"))
                        if not inputs["queries"]:
                            return NodeResult({}, "skipped")
                    else:
                        strategy = self.type.removeprefix("retrieval.")
                        approved = runtime.filter_queries([inputs.get("question", "")], record=False)
                        if not runtime.reserve_queries(approved, strategy):
                            return NodeResult({}, "skipped")
                produced = await bounded_sync(
                    lambda: self.handler(inputs, config, context), runtime.deadline, self.worker_lock,
                    cancelled=runtime.cancelled)
            else:
                # The same locks also protect ordinary runs from an agent's late worker.
                if self.worker_lock is not None:
                    from ..agentic_runtime import bounded_sync
                    produced = await bounded_sync(
                        lambda: self.handler(inputs, config, context), None, self.worker_lock)
                else:
                    produced = self.handler(inputs, config, context)
            metadata = dict(context.node_metadata)
            if isinstance(produced, Halt):
                return NodeResult(produced.outputs, "halt", metadata=metadata)
            return NodeResult(produced, "success", metadata=metadata)
        finally:
            context.node_metadata.clear()
