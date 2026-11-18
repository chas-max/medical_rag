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
    "diagnosis": "诊断标准 阈值 鉴别诊断 检查",
    "treatment": "治疗方案 干预原则 首选方案 随访",
    "medication_usage": "用法用量 禁忌证 不良反应 注意事项",
    "prognosis": "预后 转归 并发症 随访",
    "comparison": "异同 区别 适应人群 推荐等级",
}


def dialogue_retrieval_query(resolution: object) -> str:
    """A controlled retrieval expansion around the fixed semantic question."""
    base = getattr(resolution, "resolved_question", None) or ""
    suffix = INTENT_EXPANSIONS.get(getattr(resolution, "intent", ""), "")
    return f"{base} {suffix}" if suffix else base


def _find_gap_queries(question: str, evidence: list[Evidence], subqueries: list[str]) -> list[str]:
    """Identify uncovered 《guideline》 titles or distinct subqueries needing targeted retrieval."""
    from ..query_normalization import (
        extract_guideline_titles,
        extract_query_terms,
        filename_matches_title,
    )

    gaps: list[str] = []
    seen: set[str] = set()
    titles = extract_guideline_titles(question)
    for title in titles:
        if any(filename_matches_title(item.chunk.filename, [title]) for item in evidence):
            continue
        matching_sub = next((sq for sq in subqueries if title in sq), "")
        target = matching_sub or f"《{title}》 {question}".strip()
        if target not in seen:
            seen.add(target)
            gaps.append(target)

    if len(subqueries) >= 2 and evidence:
        corpus = [f"{item.chunk.filename} {item.chunk.section} {item.chunk.context_text}".lower()
                  for item in evidence]
        for sq in subqueries:
            if sq in seen:
                continue
            terms = extract_query_terms(sq)
            if not terms:
                continue
            best_cov = max(
                sum(1 for term in terms if term in doc) / len(terms)
                for doc in corpus
            )
            if best_cov < 0.35:
                seen.add(sq)
                gaps.append(sq)
    return gaps[:2]


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
        "subqueries": PortSpec("subqueries", "list<string>", optional=True),
    }
    outputs: ClassVar[dict[str, str]] = {
        "evidence": "evidence_list",
        "report": "retrieval_report",
        # Recalled conversation is context for reading the question, never evidence.
        "history": "history_context",
    }
    config_schema: ClassVar[dict[str, dict[str, Any]]] = {
        "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
        "max_rounds": {"type": "integer", "default": 1, "minimum": 0, "maximum": 1},
        "min_evidence": {"type": "integer", "default": 2, "minimum": 1, "maximum": 10},
        "threshold": {"type": "number", "default": 0.5, "minimum": 0, "maximum": 1},
        "max_per_parent": {"type": "integer", "default": 0, "minimum": 0, "maximum": 10},
        "rewrite_on_other": {"type": "boolean", "default": False},
        "check_subquery_gaps": {"type": "boolean", "default": False},
        "include_metadata": {"type": "boolean", "default": False},
        "subquery_blend_weight": {"type": "number", "default": 0.0, "minimum": 0, "maximum": 1},
        "subquery_quota": {"type": "integer", "default": 0, "minimum": 0},
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
        top_k = int(config.get("top_k") or inputs.get("top_k") or self.top_k)
        threshold = float(config.get("threshold", self.threshold))
        min_evidence = int(config.get("min_evidence", 2))
        report: dict[str, Any] = {"stop_reason": "sufficient", "rounds": 0, "queries": [],
                                  "before": len(evidence), "after": len(evidence)}
        degraded = bool(context.variables.get(RERANK_DEGRADED_VARIABLE))
        need, reason = _needs_correction(evidence, min_evidence, threshold, degraded)
        report["stop_reason"] = reason
        raw_subqueries = inputs.get("subqueries")
        subqueries = [s.strip() for s in (raw_subqueries if isinstance(raw_subqueries, list) else [])
                      if isinstance(s, str) and s.strip()]
        if not need:
            if not degraded and bool(config.get("check_subquery_gaps", False)) and int(config.get("max_rounds", 1)) >= 1:
                gap_queries = _find_gap_queries(question, evidence, subqueries)
                if gap_queries:
                    return await self._retry_gaps(question, evidence, subqueries, gap_queries,
                                                  top_k, config, context, report)
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
            if bool(config.get("rewrite_on_other", False)) and (not query or query == question.strip()):
                rewritten = await self._safe(
                    "question.rewrite",
                    {"question": rerank_question or question, "route": "rewrite",
                     "provider": provider},
                    context,
                    report,
                )
                rewritten_value = rewritten.outputs.get("query") if rewritten is not None else None
                if isinstance(rewritten_value, str) and rewritten_value.strip():
                    query = rewritten_value.strip()
                    topic = getattr(resolution, "topic", None)
                    topic_name = getattr(topic, "name", "") if topic is not None else ""
                    if topic_name and topic_name not in query:
                        query = f"{topic_name} {query}"
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
        max_per_parent = int(config.get("max_per_parent", 0))
        rerank_config = {"max_per_parent": max_per_parent} if max_per_parent > 0 else {}
        reranked = await self._safe("retrieval.rerank",
                                    {"question": rerank_question or query, "evidence": pool,
                                     "top_k": top_k},
                                    context, report, config=rerank_config)
        corrected = reranked.outputs.get("evidence") if reranked is not None else None
        report["rounds"] = 1
        if not isinstance(corrected, list) or not corrected:
            report["stop_reason"] = "retry_empty"
            return self._finish(evidence, report, history)
        report["stop_reason"] = "retried"
        return self._finish(list(corrected), report, history)

    async def _retry_gaps(
        self,
        question: str,
        evidence: list[Evidence],
        subqueries: list[str],
        gap_queries: list[str],
        top_k: int,
        config: dict[str, Any],
        context: ExecutionContext,
        report: dict[str, Any],
    ) -> NodeResult:
        """Fetch missing guideline or uncovered subquery evidence without redundant HyDE."""
        report["queries"] = list(gap_queries)
        lists: list[list[Evidence]] = []
        for gap_q in gap_queries:
            for node_type in ("retrieval.dense", "retrieval.bm25"):
                result = await self._safe(node_type, {"question": gap_q}, context, report)
                items = result.outputs.get("evidence") if result is not None else None
                if isinstance(items, list) and items:
                    lists.append(items)
        if not lists:
            report["stop_reason"] = "gap_retrieval_empty"
            return self._finish(evidence, report)
        fused = await self._safe("retrieval.rrf", {"sources": lists}, context, report)
        candidates = fused.outputs.get("evidence") if fused is not None else None
        pool = _dedupe([*evidence, *candidates]) if isinstance(candidates, list) else list(evidence)
        max_per_parent = int(config.get("max_per_parent", 0))
        rerank_config: dict[str, Any] = {
            "rerank_k": max(30, len(pool)),
            "include_metadata": bool(config.get("include_metadata", False)),
            "subquery_blend_weight": float(config.get("subquery_blend_weight", 0.0)),
            "subquery_quota": int(config.get("subquery_quota", 0)),
        }
        if max_per_parent > 0:
            rerank_config["max_per_parent"] = max_per_parent
        rerank_inputs: dict[str, Any] = {"question": question, "evidence": pool, "top_k": top_k}
        if subqueries:
            rerank_inputs["subqueries"] = subqueries
        reranked = await self._safe("retrieval.rerank", rerank_inputs, context, report, config=rerank_config)
        corrected = reranked.outputs.get("evidence") if reranked is not None else None
        report["rounds"] = 1
        if not isinstance(corrected, list) or not corrected:
            report["stop_reason"] = "gap_retry_empty"
            return self._finish(evidence, report)
        report["stop_reason"] = "gap_retried"
        return self._finish(list(corrected), report)

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
                    report: dict[str, Any], config: dict[str, Any] | None = None) -> NodeResult | None:
        """Run one registered step; a failure ends the correction, never the answer."""
        try:
            result = await self.registry.get(node_type).execute(inputs, config or {}, context)
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
