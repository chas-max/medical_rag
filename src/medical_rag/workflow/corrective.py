"""Rule-2 feedback: one bounded re-retrieval when the first pass is too weak.

The workflow graph is a DAG, so a retry cannot be an edge back to retrieval. This
node is the loop instead: it judges the reranked evidence, rewrites the question
with the same short-term context the rewriter already has, re-runs retrieval once,
and re-ranks the union. It never returns less than the evidence it was given — a
failed correction keeps the first-pass answer instead of turning it into an error.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ..types import SCORE_DENSE, SCORE_RERANK, Evidence
from .contracts import (
    RERANK_DEGRADED_VARIABLE,
    ExecutionContext,
    NodeResult,
    PortSpec,
)


def _needs_correction(evidence: list[Evidence], min_evidence: int, threshold: float,
                      degraded: bool) -> tuple[bool, str]:
    """Rule 2: too few blocks, or nothing the gate could keep.

    Only the two kinds the gate trusts may satisfy the score rule. A lexical or
    BM25 number is a different scale, so comparing it with ``threshold`` would
    invent both false alarms and false confidence.
    """
    if degraded:
        # A degraded reranker is not a retrieval problem: retrying cannot fix it,
        # and the gate is already failing closed on purpose.
        return False, "degraded"
    kept = [item for item in evidence
            if (item.score_kind == SCORE_RERANK and item.score >= threshold)
            or (item.score_kind == SCORE_DENSE and item.score >= threshold)]
    if len(evidence) < min_evidence:
        return True, "too_few"
    if not kept:
        return True, "low_score"
    return False, "sufficient"


def _dedupe(items: list[Evidence]) -> list[Evidence]:
    seen: set[str] = set()
    merged: list[Evidence] = []
    for item in items:
        if item.chunk.id in seen:
            continue
        seen.add(item.chunk.id)
        merged.append(item)
    return merged


# v7 corrects the retrieval expression only, from the intent the resolver already
# established. A free-form contextual rewrite could silently change the object or
# add a new one, which is exactly what the resolved question exists to prevent.
INTENT_EXPANSIONS = {
    "etiology": "病因 原因 发病机制",
    "daily_precautions": "日常护理 生活方式 注意事项",
    "symptoms": "症状 临床表现",
    "prevention": "预防 风险控制",
}


def dialogue_retrieval_query(resolution: object) -> str:
    """A controlled retrieval expansion around the fixed semantic question."""
    base = getattr(resolution, "resolved_question", None) or ""
    suffix = INTENT_EXPANSIONS.get(getattr(resolution, "intent", ""), "")
    return f"{base} {suffix}" if suffix else base


class CorrectiveNode:
    """One extra rewrite-and-retrieve round, composed from the registered nodes."""

    type = "retrieval.corrective"
    title = "检索反馈纠错"
    category = "retrieval"
    workflow_kinds = ("workflow",)
    inputs: ClassVar[dict[str, Any]] = {
        "question": "string",
        "evidence": "evidence_list",
        "provider": PortSpec("provider", "string", optional=True),
        "top_k": PortSpec("top_k", "integer", optional=True),
    }
    outputs: ClassVar[dict[str, str]] = {
        "evidence": "evidence_list",
        "report": "retrieval_report",
        # Recalled conversation is context for reading the question, never evidence.
        "history": "history_context",
    }
    config_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "max_rounds": {"type": "integer", "default": 1, "minimum": 0, "maximum": 1},
        "min_evidence": {"type": "integer", "default": 2, "minimum": 1, "maximum": 10},
        "threshold": {"type": "number", "default": 0.5, "minimum": 0, "maximum": 1},
    }

    def __init__(self, registry: object, transport: object = None, default_provider: str = "qwen",
                 threshold: float = 0.5, top_k: int = 5, history: object = None) -> None:
        self.registry = registry
        self.transport = transport
        self.default_provider = default_provider
        self.threshold = threshold
        self.top_k = top_k
        self.history = history

    async def execute(self, inputs: dict[str, Any], config: dict[str, Any],
                      context: ExecutionContext) -> NodeResult:
        evidence = [item for item in inputs.get("evidence") or [] if isinstance(item, Evidence)]
        question = str(inputs["question"])
        provider = inputs.get("provider") or self.default_provider
        top_k = int(inputs.get("top_k") or self.top_k)
        threshold = float(config.get("threshold", self.threshold))
        min_evidence = int(config.get("min_evidence", 2))
        report: dict[str, Any] = {"stop_reason": "sufficient", "rounds": 0, "queries": [],
                                  "before": len(evidence), "after": len(evidence)}
        need, reason = _needs_correction(evidence, min_evidence, threshold,
                                         bool(context.variables.get(RERANK_DEGRADED_VARIABLE)))
        report["stop_reason"] = reason
        if not need:
            return self._finish(evidence, report)
        if int(config.get("max_rounds", 1)) < 1:
            report["stop_reason"] = "max_rounds"
            return self._finish(evidence, report)

        # History recall belongs to rule 2 only: it runs once the first pass has
        # proved weak, never on every question.
        history = self._recall(context, question)
        report["history_turns"] = len(history)

        # A resolved dialogue already owns the object and intent, so v7 may only
        # widen the retrieval expression; v6 keeps its contextual rewrite.
        dialogue = getattr(context, "dialogue", None)
        resolution = getattr(dialogue, "resolution", None) if dialogue is not None else None
        if resolution is not None and getattr(resolution, "status", "") == "resolved":
            query = dialogue_retrieval_query(resolution)
            report["semantic_question"] = getattr(resolution, "resolved_question", "") or ""
            rerank_question = report["semantic_question"] or query
        else:
            rewritten = await self._safe("question.rewrite",
                                         {"question": question, "route": "rewrite", "provider": provider,
                                          **({"history": history} if history else {})},
                                         context, report)
            query = ""
            if rewritten is not None:
                value = rewritten.outputs.get("query")
                query = value.strip() if isinstance(value, str) else ""
            rerank_question = query
        if not query or query == question.strip():
            # Rewriting produced nothing new; a second identical retrieval would only
            # burn latency and return the same blocks.
            report["stop_reason"] = "unchanged_query"
            return self._finish(evidence, report, history)

        report["queries"] = [query]
        lists: list[list[Evidence]] = []
        for node_type in ("retrieval.dense", "retrieval.bm25", "retrieval.hyde"):
            result = await self._safe(node_type, {"question": query, "provider": provider},
                                      context, report)
            items = result.outputs.get("evidence") if result is not None else None
            if isinstance(items, list) and items:
                lists.append(items)
        if not lists:
            report["stop_reason"] = "retrieval_failed"
            return self._finish(evidence, report, history)

        fused = await self._safe("retrieval.rrf", {"sources": lists}, context, report)
        candidates = fused.outputs.get("evidence") if fused is not None else None
        pool = _dedupe([*evidence, *candidates]) if isinstance(candidates, list) else list(evidence)
        reranked = await self._safe("retrieval.rerank",
                                    {"question": rerank_question or query, "evidence": pool,
                                     "top_k": top_k},
                                    context, report)
        corrected = reranked.outputs.get("evidence") if reranked is not None else None
        report["rounds"] = 1
        if not isinstance(corrected, list) or not corrected:
            report["stop_reason"] = "retry_empty"
            return self._finish(evidence, report, history)
        report["stop_reason"] = "retried"
        return self._finish(list(corrected), report, history)

    def _recall(self, context: ExecutionContext, question: str) -> list[dict]:
        """Earlier turns of this conversation only; any failure means no recall."""
        conversation_id = getattr(context, "conversation_id", "")
        if self.history is None or not conversation_id:
            return []
        seed = question
        short = getattr(context, "short_term", None)
        recent = getattr(short, "recent", ()) if short else ()
        if recent:
            last = recent[-1]
            previous = last.get("user") if isinstance(last, dict) else ""
            if isinstance(previous, str) and previous.strip():
                seed = f"{previous.strip()} {question}"
        try:
            recalled = self.history.recall(conversation_id, seed)
        except Exception:  # noqa: BLE001 - optional index, never a reason to fail a run
            return []
        return [item for item in recalled if isinstance(item, dict) and item.get("text")]

    async def _safe(self, node_type: str, inputs: dict[str, Any], context: ExecutionContext,
                    report: dict[str, Any]) -> NodeResult | None:
        """Run one registered step; a failure ends the correction, never the answer."""
        try:
            result = await self.registry.get(node_type).execute(inputs, {}, context)
        except Exception:  # noqa: BLE001 - this is a second attempt, not the answer path
            report["failed_node"] = node_type
            return None
        return result if result.status == "success" else None

    @staticmethod
    def _finish(evidence: list[Evidence], report: dict[str, Any],
                history: list[dict] | None = None) -> NodeResult:
        report["after"] = len(evidence)
        body: dict[str, Any] = {"evidence": list(evidence), "report": dict(report)}
        if history:
            # A separate output, never merged into evidence: recalled conversation is
            # context for reading the question, not a citable medical source.
            body["history"] = [dict(item) for item in history]
        return NodeResult(body, "success", metadata={"corrective": dict(report)})
