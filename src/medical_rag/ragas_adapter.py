"""Pure conversion and merge layer between local evaluation reports and Ragas.

This module deliberately has **no** Ragas import.  Keeping the conversion,
applicability rules and report merge here means normal test runs and the main
application never need the optional evaluation dependency.

The two context views are intentional:

* ``retrieved_contexts`` is the parent-window, first-hit view the generator saw
  (``GroundedGenerator._evidence_payload`` de-duplicates by ``parent_id``).
* ``retrieved_context_ids`` keeps every retrieved child chunk id, because the
  gold ``relevant_chunk_ids`` are child ids and ID-based recall must not lose
  sibling hits under the same parent.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_METRICS = (
    "id_based_context_precision",
    "id_based_context_recall",
    "context_precision",
    "context_recall",
    "faithfulness",
    "context_relevance",
    "answer_relevancy",
)
METRIC_ALIASES = {
    "precision": "id_based_context_precision",
    "recall": "id_based_context_recall",
    "answer_relevance": "answer_relevancy",
}
LOCAL_ONLY_METRICS = {
    "mrr",
    "map",
    "ndcg",
    "core_recall",
    "hit_rate",
    "f1",
    "ap",
    "rr",
    "parent_recall",
    "parent_precision",
    "parent_ndcg",
    "post_rag_cited_precision",
    "post_rag_cited_recall",
}
SUPPORTED_METRICS = frozenset(DEFAULT_METRICS)
# Metrics whose score depends on a stochastic LLM judge. Measured run-to-run
# spread on identical input: faithfulness 0.17-0.25, context_recall 0.33,
# answer_relevancy 0.23-0.30. The statement splitter returns a different
# statement set even at a fixed count, so caching or seeding cannot fix it.
REPEAT_METRICS = (
    "faithfulness",
    "context_recall",
    "context_precision",
    "context_relevance",
    "answer_relevancy",
)
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"

# Per-case skip reasons.  These travel into the JSON report, so keep them stable.
SKIP_CASE_ERROR = "CASE_ERROR"
SKIP_EXPECTED_ABSTENTION = "EXPECTED_ABSTENTION"
SKIP_NO_RESPONSE = "NO_RESPONSE"
SKIP_NO_RETRIEVED_CONTEXTS = "NO_RETRIEVED_CONTEXTS"
SKIP_NO_REFERENCE = "NO_REFERENCE"
SKIP_NO_REFERENCE_IDS = "NO_REFERENCE_IDS"
SKIP_NO_RETRIEVED_IDS = "NO_RETRIEVED_IDS"
SKIP_LIMIT = "LIMIT_SKIPPED"

METRIC_CONTEXT_PRECISION = "context_precision"
METRIC_CONTEXT_RECALL = "context_recall"
METRIC_FAITHFULNESS = "faithfulness"
METRIC_CONTEXT_RELEVANCE = "context_relevance"
METRIC_ANSWER_RELEVANCY = "answer_relevancy"
METRIC_ID_PRECISION = "id_based_context_precision"
METRIC_ID_RECALL = "id_based_context_recall"


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        cleaned = item.strip()
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def normalize_metric_names(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """Resolve CLI metric names, aliases and the ``all`` shorthand."""
    if raw is None:
        return DEFAULT_METRICS
    if isinstance(raw, str):
        text = raw.strip().lower()
        if not text or text == "all":
            return DEFAULT_METRICS
        names = [part.strip() for part in text.split(",") if part.strip()]
    else:
        names = [str(part).strip().lower() for part in raw if str(part).strip()]
    if not names:
        raise ValueError("Ragas 指标列表不能为空")
    resolved: list[str] = []
    for name in names:
        canonical = METRIC_ALIASES.get(name, name)
        if canonical in LOCAL_ONLY_METRICS:
            raise ValueError(
                f"{name} 由本地指标计算，Ragas 0.4.4 无对应内置指标；"
                "请从 summary.mrr / summary.ndcg 等本地字段读取"
            )
        if canonical not in SUPPORTED_METRICS:
            supported = ", ".join(DEFAULT_METRICS)
            raise ValueError(f"未知 Ragas 指标: {name}；支持: {supported}")
        if canonical not in resolved:
            resolved.append(canonical)
    return tuple(resolved)


@dataclass(frozen=True)
class RagasConfig:
    """Runtime knobs for one Ragas scoring pass."""

    metrics: tuple[str, ...] = DEFAULT_METRICS
    concurrency: int = 4
    strictness: int = 3
    max_retries: int = 1
    limit: int | None = None
    timeout: float = 60.0
    max_tokens: int = 8192
    # Ragas ships English instructions with no language constraint, so the judge
    # model mixed English questions into this Chinese corpus. Language-aligned
    # prompts remove that artefact; keep the switch so an A/B run stays possible.
    chinese_prompts: bool = True
    # Repeated sampling. The judge endpoint is not seed-deterministic (an
    # explicit ``seed`` is accepted but ignored), so a single LLM-judged score
    # moves by 0.17-0.24 between runs on the same case. Sampling K times and
    # taking the median cuts that residual spread roughly in half; see
    # ``REPEAT_METRICS`` for which metrics pay the extra calls.
    repeats: int = 3
    repeat_metrics: tuple[str, ...] = REPEAT_METRICS

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", normalize_metric_names(self.metrics))
        if not 1 <= int(self.concurrency) <= 16:
            raise ValueError("Ragas 并发数必须在 1 到 16 之间")
        if not 1 <= int(self.strictness) <= 5:
            raise ValueError("Ragas answer_relevancy strictness 必须在 1 到 5 之间")
        if not 0 <= int(self.max_retries) <= 5:
            raise ValueError("Ragas 重试次数必须在 0 到 5 之间")
        if self.limit is not None and int(self.limit) < 1:
            raise ValueError("Ragas limit 必须为正整数")
        if not 0 < float(self.timeout) <= 60:
            raise ValueError("Ragas 单次调用超时必须在 0 到 60 秒之间")
        if not 256 <= int(self.max_tokens) <= 32768:
            raise ValueError("Ragas max_tokens 必须在 256 到 32768 之间")
        if not 1 <= int(self.repeats) <= 9:
            raise ValueError("Ragas 重复采样次数必须在 1 到 9 之间")
        unknown = set(self.repeat_metrics) - SUPPORTED_METRICS
        if unknown:
            raise ValueError(f"未知的重复采样指标: {', '.join(sorted(unknown))}")

    @property
    def effective_repeats(self) -> int:
        return int(self.repeats)

    def repeats_for(self, metric: str) -> int:
        """Repeat only the metrics whose judge is genuinely unstable.

        Deterministic metrics (ID-based precision/recall) are pure set
        arithmetic and always run once regardless of configuration.
        """
        if metric in (METRIC_ID_PRECISION, METRIC_ID_RECALL):
            return 1
        return int(self.repeats) if metric in self.repeat_metrics else 1

    @property
    def embedding_model(self) -> str | None:
        return EMBEDDING_MODEL if METRIC_ANSWER_RELEVANCY in self.metrics else None

    def to_report_config(self, *, model: str, provider: str = "qwen") -> dict[str, Any]:
        return {
            "metrics": list(self.metrics),
            "provider": provider,
            "model": model,
            "embedding_model": self.embedding_model,
            "concurrency": int(self.concurrency),
            "strictness": int(self.strictness),
            "max_retries": int(self.max_retries),
            "limit": self.limit,
            "chinese_prompts": bool(self.chinese_prompts),
            "repeats": int(self.repeats),
            "repeat_metrics": list(self.repeat_metrics),
        }


@dataclass(frozen=True)
class RagasSample:
    """One case converted into the two context views Ragas needs."""

    id: str
    user_input: str
    retrieved_contexts: tuple[str, ...]
    retrieved_context_ids: tuple[str, ...]
    reference_contexts: tuple[str, ...]
    reference_context_ids: tuple[str, ...]
    reference: str | None
    response: str | None
    answerability: str
    abstained: bool
    error: str | None
    retrieval_hit_count: int
    semantic_context_count: int
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _clean_response(text: object, retrieved_ids: Iterable[str]) -> str | None:
    """Drop formatting-only citation anchors, preserving the visible answer."""
    if not isinstance(text, str):
        return None
    cleaned = text
    for chunk_id in retrieved_ids:
        cleaned = cleaned.replace(f"[{chunk_id}]", "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    result = "\n".join(lines).strip()
    return result or None


def _reference_contexts(case: dict[str, Any]) -> list[str]:
    refs = case.get("source_refs")
    if not isinstance(refs, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        relevance = ref.get("relevance", 2)
        if isinstance(relevance, int) and not isinstance(relevance, bool) and relevance < 1:
            continue
        quote = ref.get("quote")
        if not _nonempty(quote):
            continue
        text = quote.strip()
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def build_sample(case: dict[str, Any]) -> RagasSample:
    """Convert one local report case into a :class:`RagasSample`.

    Raises:
        ValueError: when the case has no usable id or question.  The caller
            records this as a per-case failure rather than dropping the case.
    """
    if not isinstance(case, dict):
        raise ValueError("Ragas 用例必须是对象")
    identifier = case.get("id")
    question = case.get("question")
    if not _nonempty(identifier) or not _nonempty(question):
        raise ValueError("Ragas 用例缺少 id 或 question")

    retrieved_ids: list[str] = []
    seen_ids: set[str] = set()
    contexts: list[str] = []
    seen_contexts: set[tuple[str, str]] = set()
    parent_text: dict[tuple[str, str], str] = {}
    warnings: list[str] = []

    evidence = case.get("evidence")
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict):
            continue
        chunk_id = item.get("chunk_id")
        if not _nonempty(chunk_id):
            continue
        chunk_id = chunk_id.strip()
        if chunk_id not in seen_ids:
            seen_ids.add(chunk_id)
            retrieved_ids.append(chunk_id)
        text = item.get("text")
        if not _nonempty(text):
            continue
        text = text.strip()
        parent_id = item.get("parent_id")
        if _nonempty(parent_id):
            key = ("parent", parent_id.strip())
        else:
            # Old reports did not store parent_id.  The parent window text is
            # identical for sibling chunks, so text is the best available key.
            key = ("text", text)
        if key in seen_contexts:
            if key[0] == "parent" and parent_text.get(key) != text:
                if "PARENT_TEXT_MISMATCH" not in warnings:
                    warnings.append("PARENT_TEXT_MISMATCH")
            elif key[0] == "text" and "LEGACY_TEXT_DEDUPE" not in warnings:
                warnings.append("LEGACY_TEXT_DEDUPE")
            continue
        seen_contexts.add(key)
        parent_text[key] = text
        contexts.append(text)

    answer = case.get("answer")
    if isinstance(answer, dict):
        raw_response = answer.get("answer")
        abstained = answer.get("abstained") is True
    elif isinstance(answer, str):
        raw_response = answer
        abstained = case.get("abstained") is True
    else:
        raw_response = None
        abstained = case.get("abstained") is True
    response = None if abstained else _clean_response(raw_response, retrieved_ids)

    answerability = case.get("answerability")
    if not _nonempty(answerability):
        answerability = "insufficient_evidence" if case.get("expected_abstention") else "answerable"
    error = case.get("error")
    return RagasSample(
        id=str(identifier).strip(),
        user_input=question.strip(),
        retrieved_contexts=tuple(contexts),
        retrieved_context_ids=tuple(retrieved_ids),
        reference_contexts=tuple(_reference_contexts(case)),
        reference_context_ids=tuple(_string_list(case.get("relevant_chunk_ids"))),
        reference=(case["reference_answer"].strip() if _nonempty(case.get("reference_answer")) else None),
        response=response,
        answerability=str(answerability),
        abstained=bool(abstained),
        error=error.strip() if _nonempty(error) else None,
        retrieval_hit_count=len(retrieved_ids),
        semantic_context_count=len(contexts),
        warnings=tuple(warnings),
    )


def applicable_metrics(
    sample: RagasSample, requested: Iterable[str]
) -> tuple[list[str], dict[str, str]]:
    """Return metrics that can run and the reason each other one was skipped."""
    names = tuple(requested)
    if sample.error:
        return [], {name: SKIP_CASE_ERROR for name in names}
    if sample.answerability == "insufficient_evidence":
        return [], {name: SKIP_EXPECTED_ABSTENTION for name in names}

    applicable: list[str] = []
    skipped: dict[str, str] = {}
    for name in names:
        reason = None
        if name in (METRIC_ID_PRECISION, METRIC_ID_RECALL):
            if not sample.retrieved_context_ids:
                reason = SKIP_NO_RETRIEVED_IDS
            elif not sample.reference_context_ids:
                reason = SKIP_NO_REFERENCE_IDS
        elif name in (METRIC_CONTEXT_PRECISION, METRIC_CONTEXT_RECALL):
            if not sample.reference:
                reason = SKIP_NO_REFERENCE
            elif not sample.retrieved_contexts:
                reason = SKIP_NO_RETRIEVED_CONTEXTS
        elif name == METRIC_FAITHFULNESS:
            if not sample.response:
                reason = SKIP_NO_RESPONSE
            elif not sample.retrieved_contexts:
                reason = SKIP_NO_RETRIEVED_CONTEXTS
        elif name == METRIC_CONTEXT_RELEVANCE:
            if not sample.retrieved_contexts:
                reason = SKIP_NO_RETRIEVED_CONTEXTS
        elif name == METRIC_ANSWER_RELEVANCY:
            if not sample.response:
                reason = SKIP_NO_RESPONSE
        else:
            reason = "RAGAS_UNSUPPORTED"
        if reason is None:
            applicable.append(name)
        else:
            skipped[name] = reason
    return applicable, skipped


def build_headline(
    local_summary: dict[str, Any], ragas_summary: dict[str, Any]
) -> tuple[dict[str, float | None], dict[str, str]]:
    """Build the unified headline, preferring Ragas and falling back locally.

    ``0.0`` is a real score, so every check is an explicit ``is not None`` test
    rather than a truthiness fallback.
    """

    def pick(primary: object, fallback: object) -> tuple[float | None, str]:
        if _finite(primary):
            return float(primary), "ragas"
        if _finite(fallback):
            return float(fallback), "local"
        return None, "unavailable"

    local = local_summary if isinstance(local_summary, dict) else {}
    ragas = ragas_summary if isinstance(ragas_summary, dict) else {}
    specs: list[tuple[str, object, object]] = [
        ("precision", ragas.get(METRIC_ID_PRECISION), local.get("precision")),
        ("recall", ragas.get(METRIC_ID_RECALL), local.get("recall")),
        ("faithfulness", ragas.get(METRIC_FAITHFULNESS), local.get("faithfulness")),
        ("context_precision", ragas.get(METRIC_CONTEXT_PRECISION), None),
        ("context_recall", ragas.get(METRIC_CONTEXT_RECALL), None),
        ("context_relevance", ragas.get(METRIC_CONTEXT_RELEVANCE), local.get("context_relevance")),
        ("answer_relevance", ragas.get(METRIC_ANSWER_RELEVANCY), local.get("answer_relevance")),
        ("mrr", None, local.get("mrr")),
        ("map", None, local.get("map")),
        ("ndcg", None, local.get("ndcg")),
        ("core_recall", None, local.get("core_recall")),
        ("hit_rate", None, local.get("hit_rate")),
    ]
    headline: dict[str, float | None] = {}
    sources: dict[str, str] = {}
    for name, primary, fallback in specs:
        value, source = pick(primary, fallback)
        headline[name] = value
        sources[name] = source
    return headline, sources


def _update_judge_summary(
    summary: dict[str, Any],
    values: dict[str, list[float]],
) -> None:
    valid_counts = summary.get("valid_counts")
    if not isinstance(valid_counts, dict):
        valid_counts = {}
        summary["valid_counts"] = valid_counts
    for name, scores in values.items():
        if scores:
            summary[name] = _mean(scores)
            valid_counts[name] = len(scores)


def merge_ragas_report(
    report: dict[str, Any],
    block: dict[str, Any],
    *,
    overwrite_judge_fields: bool = True,
    model: str | None = None,
) -> dict[str, Any]:
    """Merge a Ragas block into a local report without inventing scores."""
    if not isinstance(report, dict) or not isinstance(report.get("cases"), list):
        raise ValueError("报告必须是含 cases 数组的对象")
    if not isinstance(block, dict) or not isinstance(block.get("cases"), list):
        raise ValueError("Ragas 结果必须是含 cases 数组的对象")
    report_cases = report["cases"]
    block_cases = block["cases"]
    report_ids = [case.get("id") for case in report_cases if isinstance(case, dict)]
    if len(report_ids) != len(set(report_ids)):
        raise ValueError("报告题目 ID 重复")
    by_id: dict[str, dict[str, Any]] = {}
    for item in block_cases:
        if not isinstance(item, dict) or not _nonempty(item.get("id")):
            raise ValueError("Ragas 结果题目 ID 无效")
        identifier = str(item["id"]).strip()
        if identifier in by_id:
            raise ValueError("Ragas 结果题目 ID 重复")
        by_id[identifier] = item
    unknown = set(by_id) - {str(item) for item in report_ids}
    if unknown:
        raise ValueError("Ragas 结果包含报告之外的题目")

    # Attach the compact per-case view even when judge fields are left untouched,
    # so a report can be read without cross-indexing report["ragas"]["cases"].
    for case in report_cases:
        if not isinstance(case, dict):
            continue
        item = by_id.get(str(case.get("id")))
        if item is None:
            continue
        case["ragas"] = {
            "scores": dict(item.get("scores") or {}),
            "errors": dict(item.get("errors") or {}),
            "skipped": dict(item.get("skipped") or {}),
            "retrieval_hit_count": item.get("retrieval_hit_count"),
            "semantic_context_count": item.get("semantic_context_count"),
            "warnings": list(item.get("warnings") or []),
        }

    local_summary = dict(report.get("summary") or {})
    report["ragas"] = block
    config = report.get("config")
    if not isinstance(config, dict):
        config = {}
        report["config"] = config
    config["ragas"] = block.get("config") or {}

    summary = report.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        report["summary"] = summary
    ragas_summary = block.get("summary") if isinstance(block.get("summary"), dict) else {}
    summary["ragas"] = ragas_summary
    headline, sources = build_headline(local_summary, ragas_summary)
    summary["headline"] = headline
    summary["metric_sources"] = sources
    failures = block.get("failure_counts")
    if isinstance(failures, dict):
        summary["ragas_metric_failures"] = sum(
            int(value) for value in failures.values() if isinstance(value, int)
        )

    if overwrite_judge_fields:
        model_name = (
            model
            or (block.get("config") or {}).get("model")
            or "qwen"
        )
        values: dict[str, list[float]] = {
            METRIC_FAITHFULNESS: [],
            METRIC_CONTEXT_RELEVANCE: [],
            METRIC_ANSWER_RELEVANCY: [],
        }
        for case in report_cases:
            if not isinstance(case, dict):
                continue
            item = by_id.get(str(case.get("id")))
            if item is None:
                continue
            scores = item.get("scores")
            if not isinstance(scores, dict):
                continue
            faithfulness = scores.get(METRIC_FAITHFULNESS)
            if _finite(faithfulness):
                value = float(faithfulness)
                case["faithfulness"] = {
                    "score": value,
                    "reason": None,
                    "model": model_name,
                    "source": "ragas",
                }
                values[METRIC_FAITHFULNESS].append(value)
            context_relevance = scores.get(METRIC_CONTEXT_RELEVANCE)
            if _finite(context_relevance):
                value = float(context_relevance)
                case["context_relevance"] = {
                    "score": value,
                    "items": [],
                    "model": model_name,
                    "source": "ragas",
                }
                values[METRIC_CONTEXT_RELEVANCE].append(value)
            answer_relevance = scores.get(METRIC_ANSWER_RELEVANCY)
            if _finite(answer_relevance):
                value = float(answer_relevance)
                case["answer_relevance"] = {
                    "score": value,
                    "reason": None,
                    "model": model_name,
                    "source": "ragas",
                }
                values[METRIC_ANSWER_RELEVANCY].append(value)
        # Map Ragas metric keys back to the local judge field names.
        _update_judge_summary(
            summary,
            {
                "faithfulness": values[METRIC_FAITHFULNESS],
                "context_relevance": values[METRIC_CONTEXT_RELEVANCE],
                "answer_relevance": values[METRIC_ANSWER_RELEVANCY],
            },
        )
    return report
