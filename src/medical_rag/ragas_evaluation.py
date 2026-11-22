"""Ragas scoring runner.

This module is the only place that imports Ragas.  The main application and
normal test suite never import it; ``evaluate.py`` imports it lazily only when
``--ragas`` is requested.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
import time
import warnings
from typing import Any, Iterable

# Must be set before Ragas sends any analytics event.
os.environ["RAGAS_DO_NOT_TRACK"] = "True"

import ragas  # noqa: E402
from ragas.dataset_schema import SingleTurnSample  # noqa: E402
from ragas.metrics.collections import (  # noqa: E402
    AnswerRelevancy,
    ContextPrecision,
    ContextRecall,
    ContextRelevance,
    Faithfulness,
)

with warnings.catch_warnings():
    # Ragas 0.4.4 has not moved the ID-based metrics into collections yet.
    # Keep the public legacy imports (rather than private modules) and suppress
    # the deprecation warning in this one place.
    warnings.simplefilter("ignore", DeprecationWarning)
    from ragas.metrics import (  # noqa: E402
        IDBasedContextPrecision,
        IDBasedContextRecall,
    )

from .ragas_adapter import (  # noqa: E402
    METRIC_ANSWER_RELEVANCY,
    METRIC_CONTEXT_PRECISION,
    METRIC_CONTEXT_RECALL,
    METRIC_CONTEXT_RELEVANCE,
    METRIC_FAITHFULNESS,
    METRIC_ID_PRECISION,
    METRIC_ID_RECALL,
    RagasConfig,
    RagasSample,
    SKIP_LIMIT,
    applicable_metrics,
    build_sample,
)
from .ragas_llm import BgeRagasEmbedding, QwenRagasLLM  # noqa: E402
from .ragas_prompts import apply_chinese_prompts  # noqa: E402

_RAGAS_LOGGER = logging.getLogger("ragas")
_INIT_FAILED = "METRIC_INIT_FAILED"
_EMBEDDINGS_UNAVAILABLE = "EMBEDDINGS_UNAVAILABLE"
_LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
_METRIC_FAILED = "METRIC_FAILED"
_METRIC_NAN = "METRIC_NAN"
# Internal-only: a transport hiccup, retried before it can reach the report.
# Never persisted, so the emitted error codes stay stable.
_TRANSIENT = "METRIC_TRANSIENT"
_CASE_INVALID = "CASE_INVALID"
_ID_METRICS = {METRIC_ID_PRECISION, METRIC_ID_RECALL}

# Upper bound on extra attempts for one transient failure. The endpoint drops
# connections under sustained concurrency; without this a whole report can lose
# metrics to a few seconds of network noise.
_TRANSIENT_ATTEMPTS = 3
_TRANSIENT_BACKOFF_SECONDS = 1.5


def _transient_error_types() -> tuple[type, ...]:
    """Transport/connection errors that are worth retrying.

    Resolution is lazy and tolerant so importing this module stays cheap.
    ``ApiError`` is deliberately excluded here: it wraps both transient and
    permanent model faults, so it is filtered by code in ``_is_transient``.
    """
    types: list[type] = [ConnectionError, TimeoutError, OSError]
    try:
        from openai import APIConnectionError, APITimeoutError, RateLimitError

        types.extend([APIConnectionError, APITimeoutError, RateLimitError])
    except Exception:  # noqa: BLE001 - optional dependency
        pass
    return tuple(types)


_TRANSIENT_ERRORS = _transient_error_types()
# Model faults worth another attempt; authentication/configuration are not.
_TRANSIENT_API_CODES = frozenset(
    {"MODEL_UNAVAILABLE", "MODEL_RATE_LIMITED", "MODEL_UPSTREAM_ERROR"}
)


def _is_transient(error: Exception) -> bool:
    if isinstance(error, _TRANSIENT_ERRORS):
        return True
    return getattr(error, "code", None) in _TRANSIENT_API_CODES


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _quiet_ragas_logger() -> tuple[bool, list[logging.Handler], int]:
    old = (_RAGAS_LOGGER.propagate, list(_RAGAS_LOGGER.handlers), _RAGAS_LOGGER.level)
    _RAGAS_LOGGER.propagate = False
    _RAGAS_LOGGER.handlers = [logging.NullHandler()]
    _RAGAS_LOGGER.setLevel(logging.CRITICAL)
    return old


def _restore_ragas_logger(old: tuple[bool, list[logging.Handler], int]) -> None:
    _RAGAS_LOGGER.propagate, handlers, _RAGAS_LOGGER.level = old
    _RAGAS_LOGGER.handlers = handlers


def build_ragas_llm(settings: object, config: RagasConfig) -> QwenRagasLLM:
    """Build the Qwen adapter from the project's existing transport."""
    api_key = getattr(settings, "qwen_api_key", "")
    if not api_key:
        raise ValueError("MODEL_NOT_CONFIGURED")
    from .generation import OpenAITransport

    return QwenRagasLLM(
        OpenAITransport(settings),  # type: ignore[arg-type]
        model=str(getattr(settings, "qwen_model", "")),
        provider="qwen",
        timeout=config.timeout,
        max_tokens=config.max_tokens,
    )


def build_ragas_embeddings(settings: object | None = None) -> BgeRagasEmbedding:
    """Build the local BGE embedding adapter; the model loads lazily."""
    from .embeddings import BgeEmbedder

    return BgeRagasEmbedding(BgeEmbedder())


def build_metrics(
    llm: object | None,
    embeddings: object | None,
    config: RagasConfig,
) -> tuple[dict[str, object], dict[str, str]]:
    """Construct selected metrics; one bad metric must not abort the others."""
    metrics: dict[str, object] = {}
    errors: dict[str, str] = {}
    for name in config.metrics:
        if name not in _ID_METRICS and llm is None:
            errors[name] = _LLM_UNAVAILABLE
            continue
        try:
            if name == METRIC_FAITHFULNESS:
                metrics[name] = Faithfulness(llm=llm)  # type: ignore[arg-type]
            elif name == METRIC_CONTEXT_PRECISION:
                metrics[name] = ContextPrecision(llm=llm)  # type: ignore[arg-type]
            elif name == METRIC_CONTEXT_RECALL:
                metrics[name] = ContextRecall(llm=llm)  # type: ignore[arg-type]
            elif name == METRIC_CONTEXT_RELEVANCE:
                metrics[name] = ContextRelevance(  # type: ignore[arg-type]
                    llm=llm, max_retries=config.max_retries
                )
            elif name == METRIC_ANSWER_RELEVANCY:
                if embeddings is None:
                    errors[name] = _EMBEDDINGS_UNAVAILABLE
                    continue
                metrics[name] = AnswerRelevancy(  # type: ignore[arg-type]
                    llm=llm,
                    embeddings=embeddings,
                    strictness=config.strictness,
                )
            elif name == METRIC_ID_PRECISION:
                metrics[name] = IDBasedContextPrecision()
            elif name == METRIC_ID_RECALL:
                metrics[name] = IDBasedContextRecall()
            else:  # normalize_metric_names prevents this; keep a safe fallback.
                errors[name] = "RAGAS_UNSUPPORTED"
        except Exception:  # noqa: BLE001 - a failed metric is isolated below
            errors[name] = _INIT_FAILED
    if config.chinese_prompts:
        # The English default makes qwen3.8-flash generate English questions for
        # Chinese answers; the Chinese-only BGE then scores cross-lingual pairs
        # near 0.4 cosine similarity. Swap in language-aligned instructions.
        apply_chinese_prompts(metrics)
    return metrics, errors


def _invalid_sample(case: dict[str, Any]) -> RagasSample:
    identifier = case.get("id")
    return RagasSample(
        id=str(identifier).strip() if isinstance(identifier, str) and identifier.strip() else "?",
        user_input="",
        retrieved_contexts=(),
        retrieved_context_ids=(),
        reference_contexts=(),
        reference_context_ids=(),
        reference=None,
        response=None,
        answerability="answerable",
        abstained=False,
        error=_CASE_INVALID,
        retrieval_hit_count=0,
        semantic_context_count=0,
        warnings=(),
    )


async def _invoke_metric(
    name: str, metric: object, sample: RagasSample
) -> tuple[float | None, str | None]:
    try:
        if name in (METRIC_ID_PRECISION, METRIC_ID_RECALL):
            result = await metric.single_turn_ascore(  # type: ignore[attr-defined]
                SingleTurnSample(
                    retrieved_context_ids=list(sample.retrieved_context_ids),
                    reference_context_ids=list(sample.reference_context_ids),
                ),
                [],
            )
        else:
            contexts = list(sample.retrieved_contexts)
            if name == METRIC_FAITHFULNESS:
                result = await metric.ascore(  # type: ignore[attr-defined]
                    user_input=sample.user_input,
                    response=sample.response,
                    retrieved_contexts=contexts,
                )
            elif name == METRIC_CONTEXT_PRECISION:
                result = await metric.ascore(  # type: ignore[attr-defined]
                    user_input=sample.user_input,
                    reference=sample.reference,
                    retrieved_contexts=contexts,
                )
            elif name == METRIC_CONTEXT_RECALL:
                result = await metric.ascore(  # type: ignore[attr-defined]
                    user_input=sample.user_input,
                    retrieved_contexts=contexts,
                    reference=sample.reference,
                )
            elif name == METRIC_CONTEXT_RELEVANCE:
                result = await metric.ascore(  # type: ignore[attr-defined]
                    user_input=sample.user_input,
                    retrieved_contexts=contexts,
                )
            elif name == METRIC_ANSWER_RELEVANCY:
                result = await metric.ascore(  # type: ignore[attr-defined]
                    user_input=sample.user_input,
                    response=sample.response,
                )
            else:
                return None, "RAGAS_UNSUPPORTED"
        value = getattr(result, "value", result)
        numeric = _number(value)
        return (numeric, None) if numeric is not None else (None, _METRIC_NAN)
    except Exception as error:  # noqa: BLE001 - never persist provider/prompt content
        # Separate a transport hiccup from a judge that genuinely cannot score.
        # Only the class/code is inspected; the message may carry provider text.
        if _is_transient(error):
            return None, _TRANSIENT
        return None, _METRIC_FAILED


async def _invoke_with_retry(
    name: str, metric: object, sample: RagasSample, semaphore: asyncio.Semaphore
) -> tuple[float | None, str | None]:
    """One draw, retried on transient transport errors.

    Under sustained concurrency the endpoint occasionally drops a connection.
    Those failures are independent of the case being scored, so retrying costs
    a few seconds and keeps them out of the report instead of silently shrinking
    the valid-sample count.
    """
    for attempt in range(_TRANSIENT_ATTEMPTS):
        async with semaphore:
            score, error = await _invoke_metric(name, metric, sample)
        if error != _TRANSIENT:
            return score, error
        if attempt + 1 < _TRANSIENT_ATTEMPTS:
            await asyncio.sleep(_TRANSIENT_BACKOFF_SECONDS * (attempt + 1))
    # Out of attempts: leave it unretryable with the stable public code.
    return None, _METRIC_FAILED


async def _repeat_median(
    name: str,
    metric: object,
    sample: RagasSample,
    tries: int,
    semaphore: asyncio.Semaphore,
) -> tuple[float | None, str | None]:
    """Sample a stochastic judge ``tries`` times and keep the median.

    The endpoint is not seed-deterministic, so one draw is not reproducible;
    the median of several draws is. Each draw already retries transient
    transport errors, so a failure here means the judge really could not score.
    """
    values: list[float] = []
    errors: list[str] = []

    outcomes = await asyncio.gather(
        *(_invoke_with_retry(name, metric, sample, semaphore) for _ in range(tries))
    )
    for score, error in outcomes:
        if score is not None:
            values.append(score)
        elif error is not None:
            errors.append(error)
    if not values:
        # Every draw failed: report the single most common cause.
        if errors:
            return None, max(set(errors), key=errors.count)
        return None, _METRIC_NAN
    return statistics.median(values), None


async def score_samples(
    samples: list[RagasSample],
    metrics: dict[str, object],
    config: RagasConfig,
    *,
    init_errors: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Score all cases with bounded concurrency and per-metric isolation."""
    init_errors = init_errors or {}
    semaphore = asyncio.Semaphore(max(1, int(config.concurrency)))
    results: list[dict[str, Any]] = []
    tasks: list[tuple[int, str, asyncio.Task[tuple[float | None, str | None]]]] = []

    for index, sample in enumerate(samples):
        case_result: dict[str, Any] = {
            "id": sample.id,
            "retrieval_hit_count": sample.retrieval_hit_count,
            "semantic_context_count": sample.semantic_context_count,
            "scores": {},
            "errors": {},
            "skipped": {},
            "warnings": list(sample.warnings),
            # Which metrics were averaged over repeated draws, and how many.
            "repeats": {
                name: config.repeats_for(name) for name in config.metrics
                if config.repeats_for(name) > 1
            },
        }
        results.append(case_result)
        if config.limit is not None and index >= int(config.limit):
            case_result["skipped"] = {name: SKIP_LIMIT for name in config.metrics}
            continue
        applicable, skipped = applicable_metrics(sample, config.metrics)
        case_result["skipped"].update(skipped)
        if skipped and len(skipped) == len(config.metrics):
            # Case-level skips (error / expected abstention) take precedence over
            # initialization errors: there is nothing to score either way.
            continue
        for name in config.metrics:
            if name in skipped:
                continue
            if name in init_errors:
                case_result["errors"][name] = init_errors[name]
                continue
            metric = metrics.get(name)
            if metric is None:
                case_result["errors"][name] = _INIT_FAILED
                continue

            async def run_one(
                metric_name: str = name,
                metric_object: object = metric,
                current: RagasSample = sample,
            ) -> tuple[float | None, str | None]:
                if metric_name in (METRIC_ID_PRECISION, METRIC_ID_RECALL):
                    return await _invoke_metric(metric_name, metric_object, current)
                tries = config.repeats_for(metric_name)
                if tries <= 1:
                    return await _invoke_with_retry(
                        metric_name, metric_object, current, semaphore
                    )
                return await _repeat_median(
                    metric_name, metric_object, current, tries, semaphore
                )

            tasks.append((index, name, asyncio.create_task(run_one())))

    if tasks:
        outcomes = await asyncio.gather(*(task for _, _, task in tasks))
        for (index, name, _), (score, error) in zip(tasks, outcomes):
            case_result = results[index]
            if error is not None:
                case_result["errors"][name] = error
            elif score is not None:
                case_result["scores"][name] = score
            else:
                case_result["errors"][name] = _METRIC_NAN
    return results


def _summarize(
    results: list[dict[str, Any]], metric_names: Iterable[str]
) -> tuple[dict[str, float | None], dict[str, int], dict[str, int], dict[str, int]]:
    names = tuple(metric_names)
    scores = {name: [] for name in names}
    failure_counts: dict[str, int] = {}
    skip_counts: dict[str, int] = {}
    for row in results:
        row_scores = row.get("scores") or {}
        for name in names:
            value = row_scores.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric = float(value)
                if math.isfinite(numeric):
                    scores[name].append(numeric)
        for code in (row.get("errors") or {}).values():
            failure_counts[code] = failure_counts.get(code, 0) + 1
        for code in (row.get("skipped") or {}).values():
            skip_counts[code] = skip_counts.get(code, 0) + 1
    summary = {
        name: (sum(values) / len(values) if values else None)
        for name, values in scores.items()
    }
    valid_counts = {name: len(values) for name, values in scores.items()}
    return summary, valid_counts, failure_counts, skip_counts


async def run_ragas_evaluation_async(
    report: dict[str, Any],
    config: RagasConfig,
    *,
    settings: object | None = None,
    llm: object | None = None,
    embeddings: object | None = None,
    metrics: dict[str, object] | None = None,
    init_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one Ragas pass and return the JSON-serializable ``ragas`` block."""
    if not isinstance(report, dict) or not isinstance(report.get("cases"), list):
        raise ValueError("报告必须是含 cases 数组的对象")
    samples: list[RagasSample] = []
    for case in report["cases"]:
        if not isinstance(case, dict):
            samples.append(_invalid_sample({}))
            continue
        try:
            samples.append(build_sample(case))
        except ValueError:
            samples.append(_invalid_sample(case))

    needs_llm = any(name not in _ID_METRICS for name in config.metrics)
    if llm is None and needs_llm:
        if settings is None:
            raise ValueError("RAGAS_SETTINGS_REQUIRED")
        llm = build_ragas_llm(settings, config)
    if metrics is None:
        if embeddings is None and METRIC_ANSWER_RELEVANCY in config.metrics:
            try:
                embeddings = build_ragas_embeddings(settings)
            except Exception:  # noqa: BLE001 - answer_relevancy is optional
                embeddings = None
        built, built_errors = build_metrics(llm, embeddings, config)
        if init_errors:
            built_errors = {**init_errors, **built_errors}
        else:
            init_errors = built_errors
        metrics = built

    started = time.perf_counter()
    old_logger = _quiet_ragas_logger()
    try:
        results = await score_samples(samples, metrics, config, init_errors=init_errors)
    finally:
        _restore_ragas_logger(old_logger)
    duration_ms = (time.perf_counter() - started) * 1000
    summary, valid_counts, failure_counts, skip_counts = _summarize(results, config.metrics)
    scored_count = sum(1 for row in results if row["scores"])
    model = str(getattr(llm, "model", "") or getattr(settings, "qwen_model", "unknown"))
    provider = str(getattr(llm, "provider", "qwen"))
    return {
        "enabled": True,
        "version": str(getattr(ragas, "__version__", "unknown")),
        "config": config.to_report_config(model=model, provider=provider),
        "sample_count": len(samples),
        "scored_count": scored_count,
        "duration_ms": duration_ms,
        "summary": summary,
        "valid_counts": valid_counts,
        "failure_counts": failure_counts,
        "skip_counts": skip_counts,
        "cases": results,
    }


def run_ragas_evaluation(
    report: dict[str, Any],
    config: RagasConfig,
    *,
    settings: object | None = None,
    llm: object | None = None,
    embeddings: object | None = None,
    metrics: dict[str, object] | None = None,
    init_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Synchronous entry point used by the CLIs."""
    return asyncio.run(
        run_ragas_evaluation_async(
            report,
            config,
            settings=settings,
            llm=llm,
            embeddings=embeddings,
            metrics=metrics,
            init_errors=init_errors,
        )
    )
