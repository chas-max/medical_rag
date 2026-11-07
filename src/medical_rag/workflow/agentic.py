"""Bounded controller: every iteration executes the user's published DAG."""

import asyncio
from dataclasses import asdict
from time import monotonic
from typing import Any, ClassVar

from ..types import ApiError
from .agentic_runtime import AgentRuntime, normalized
from .contracts import ExecutionContext, NodeResult, PortSpec
from .engine import WorkflowEngine
from .registry import NodeRegistry
from .repository import WorkflowRepository
from .validator import validate_workflow


class AgenticNode:
    """Execute pinned child graphs under one stage-wide deadline."""

    type = "retrieval.agentic"
    title = "本地检索纠错"
    category = "retrieval"
    workflow_kinds = ("workflow",)
    inputs: ClassVar[dict[str, str | PortSpec]] = {
        "question": "string",
        "evidence": "evidence_list",
        "provider": PortSpec("provider", "string", optional=True),
        "top_k": PortSpec("top_k", "integer", optional=True),
    }
    outputs: ClassVar[dict[str, str]] = {
        "evidence": "evidence_list",
        "report": "agent_report",
    }
    config_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "workflow_id": {"type": "string", "default": "local-retrieval-correction"},
        "workflow_version": {"type": "integer", "minimum": 1},
        "max_rounds": {"type": "integer", "default": 2, "minimum": 0, "maximum": 2},
        "max_queries": {"type": "integer", "default": 2, "minimum": 1, "maximum": 2},
        "timeout_seconds": {
            "type": "number",
            "default": 60,
            "minimum": 0.000001,
            "maximum": 60,
        },
    }

    def __init__(self, repository: WorkflowRepository, registry: NodeRegistry) -> None:
        self.repository, self.registry = repository, registry

    async def execute(
        self, inputs: dict[str, Any], config: dict[str, Any], context: ExecutionContext
    ) -> NodeResult:
        """Return last valid wired evidence and a safe bounded execution report."""
        from .agentic_validation import config_valid, reference_child

        if not config_valid(config, self.config_schema):
            raise ApiError("INVALID_WORKFLOW", "纠错容器配置无效", 422)
        child = reference_child(config, self.repository)
        if child is None or validate_workflow(child, self.registry):
            raise ApiError("INVALID_WORKFLOW", "迭代子图校验未通过", 422)
        rounds = int(config.get("max_rounds", 2))
        runtime = AgentRuntime(
            inputs["question"],
            monotonic() + float(config.get("timeout_seconds", 60)),
            int(config.get("max_queries", 2)),
            context.node_id or self.type,
            context.emit,
        )
        runtime.seen.add(normalized(runtime.question))
        runtime.evidence = list(inputs["evidence"])
        runtime.candidates = list(runtime.evidence)
        engine = WorkflowEngine(self.registry, context.capabilities)
        iterations, retrieval_rounds, reason = [], 0, "max_rounds"
        feedback = {}
        for iteration in range(rounds + 1):
            runtime.iteration, runtime.allow_repair = iteration, iteration < rounds
            runtime.queries, runtime.child_trace = [], []
            runtime.dispatched = set()
            runtime.retrieved = []
            runtime.assessment = None
            before = {item.chunk.id for item in runtime.candidates}
            started = monotonic()
            try:
                result = await asyncio.wait_for(
                    engine.run(
                        child,
                        {
                            **inputs,
                            "question": runtime.question,
                            "evidence": list(runtime.evidence),
                            "feedback": feedback,
                            "_agent_runtime": runtime,
                        },
                        memory_context=context.memory_context,
                        short_term=context.short_term,
                    ),
                    max(0.000001, runtime.deadline - monotonic()),
                )
            except TimeoutError:
                result = None
            trace = result.trace if result is not None else runtime.child_trace
            if runtime.dispatched:
                retrieval_rounds += 1
            iterations.append(
                {
                    "iteration": iteration,
                    "queries": list(runtime.queries),
                    "evidence_ids": [item.chunk.id for item in runtime.evidence],
                    "duration_ms": max(0, int((monotonic() - started) * 1000)),
                    "trace": [asdict(entry) for entry in trace],
                }
            )
            if runtime.assessment is not None:
                iterations[-1]["assessment"] = runtime.assessment
            if result is None or monotonic() >= runtime.deadline:
                reason = "timeout"
                break
            if result.status == "failed":
                failed = next(
                    entry.node_id for entry in result.trace if entry.status == "failed"
                )
                node_type = next(node.type for node in child.nodes if node.id == failed)
                reason = {
                    "evidence.assess": "assessment_failed",
                    "question.refine": "planning_failed",
                }.get(node_type, "retrieval_failed")
                break
            assessment = result.output.get("assessment")
            if not isinstance(assessment, dict):
                reason = "assessment_failed"
                break
            if assessment["sufficient"]:
                reason = "sufficient"
                break
            if not runtime.allow_repair:
                reason = "max_rounds"
                break
            if runtime.stop_reason:
                reason = runtime.stop_reason
                break
            if not runtime.queries:
                reason = "planning_failed"
                break
            if not ({item.chunk.id for item in runtime.retrieved} - before):
                reason = "no_new_evidence"
                break
            runtime.evidence = list(result.output["evidence"])
            from .nodes.agent import deduplicate

            runtime.candidates = deduplicate([runtime.candidates, runtime.retrieved])
            iterations[-1]["evidence_ids"] = [
                item.chunk.id for item in runtime.evidence
            ]
            feedback = result.output.get("feedback", {})
        report = {
            "stop_reason": reason,
            "retrieval_rounds": retrieval_rounds,
            "model_calls": runtime.model_calls,
            "iterations": iterations,
        }
        runtime.progress("stopped", reason)
        runtime.active = False
        return NodeResult(
            {"evidence": list(runtime.evidence), "report": report},
            "success",
            metadata={"agentic": report},
        )
