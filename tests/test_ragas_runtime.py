import asyncio
import logging
import math
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("ragas")

from ragas.llms.base import InstructorBaseRagasLLM  # noqa: E402

from medical_rag.ragas_adapter import RagasConfig, RagasSample  # noqa: E402
from medical_rag.ragas_evaluation import (  # noqa: E402
    build_metrics,
    run_ragas_evaluation_async,
    score_samples,
)
from medical_rag.ragas_llm import BgeRagasEmbedding, QwenRagasLLM  # noqa: E402


class Verdict:
    pass


def response_model():
    from pydantic import BaseModel

    class VerdictModel(BaseModel):
        verdict: int
        reason: str = ""

    return VerdictModel


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete_with_timeout(self, provider, system, prompt, timeout, *,
                              json_mode=False, max_tokens=None):
        self.calls.append({
            "provider": provider,
            "prompt": prompt,
            "timeout": timeout,
            "json_mode": json_mode,
            "max_tokens": max_tokens,
        })
        if not self.replies:
            raise RuntimeError("no reply")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeLLM(InstructorBaseRagasLLM):
    model = "qwen3.8-flash"
    provider = "qwen"
    is_async = True

    def generate(self, prompt, response_model):
        raise NotImplementedError

    async def agenerate(self, prompt, response_model):
        raise NotImplementedError


def test_qwen_ragas_llm_parses_clean_and_fenced_json():
    model = response_model()
    transport = FakeTransport(['{"verdict": 1, "reason": "ok"}'])
    llm = QwenRagasLLM(transport, model="qwen3.8-flash")
    result = llm.generate("prompt", model)
    assert result.verdict == 1
    assert transport.calls[0]["json_mode"] is True
    assert transport.calls[0]["provider"] == "qwen"

    fenced = FakeTransport(["```json\n{\"verdict\": 0}\n```"])
    assert QwenRagasLLM(fenced, model="qwen3.8-flash").generate("p", model).verdict == 0


def test_qwen_ragas_llm_retries_once_then_fails_safely():
    model = response_model()
    transport = FakeTransport(["not json", '{"verdict": 1}'])
    result = QwenRagasLLM(transport, model="qwen3.8-flash").generate("prompt", model)
    assert result.verdict == 1
    assert len(transport.calls) == 2

    invalid = FakeTransport(["not json", "still not json"])
    with pytest.raises(ValueError, match="RAGAS_LLM_OUTPUT_INVALID"):
        QwenRagasLLM(invalid, model="qwen3.8-flash").generate("prompt", model)


def test_qwen_ragas_llm_async_and_api_error_passthrough():
    model = response_model()
    transport = FakeTransport(['{"verdict": 1}'])
    result = asyncio.run(QwenRagasLLM(transport, model="qwen3.8-flash").agenerate("p", model))
    assert result.verdict == 1

    error = FakeTransport([RuntimeError("secret credential")])
    with pytest.raises(RuntimeError, match="secret credential"):
        QwenRagasLLM(error, model="qwen3.8-flash").generate("p", model)


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        return [[float(index), 1.0] for index, _ in enumerate(texts)]


def test_bge_embedding_adapter_sync_and_async():
    embedder = FakeEmbedder()
    adapter = BgeRagasEmbedding(embedder)
    assert adapter.embed_text("a") == [0.0, 1.0]
    assert adapter.embed_texts(["a", "b"]) == [[0.0, 1.0], [1.0, 1.0]]
    assert asyncio.run(adapter.aembed_text("a")) == [0.0, 1.0]
    assert asyncio.run(adapter.aembed_texts(["a", "b"])) == [[0.0, 1.0], [1.0, 1.0]]


def test_build_metrics_selects_expected_objects_and_isolates_missing_embeddings():
    config = RagasConfig(metrics=(
        "faithfulness", "context_precision", "context_recall", "context_relevance",
        "id_based_context_precision", "id_based_context_recall", "answer_relevancy",
    ))
    metrics, errors = build_metrics(FakeLLM(), None, config)
    assert set(metrics) == {
        "faithfulness", "context_precision", "context_recall", "context_relevance",
        "id_based_context_precision", "id_based_context_recall",
    }
    assert errors == {"answer_relevancy": "EMBEDDINGS_UNAVAILABLE"}


class FakeScoredMetric:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = []

    async def ascore(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(value=self.value)


class FakeIdMetric:
    def __init__(self, value):
        self.value = value
        self.samples = []

    async def single_turn_ascore(self, sample, callbacks):
        self.samples.append(sample)
        return self.value


def _sample():
    return RagasSample(
        id="1",
        user_input="q",
        retrieved_contexts=("父窗口",),
        retrieved_context_ids=("c1", "c2"),
        reference_contexts=("参考",),
        reference_context_ids=("c1",),
        reference="参考回答",
        response="回答",
        answerability="answerable",
        abstained=False,
        error=None,
        retrieval_hit_count=2,
        semantic_context_count=1,
    )


def test_score_samples_uses_parent_view_for_semantics_and_all_ids_for_id_metrics():
    semantic = FakeScoredMetric(0.75)
    id_metric = FakeIdMetric(0.5)
    config = RagasConfig(metrics=("context_relevance", "id_based_context_precision"),
                         concurrency=2)
    results = asyncio.run(score_samples(
        [_sample()],
        {
            "context_relevance": semantic,
            "id_based_context_precision": id_metric,
        },
        config,
    ))
    assert semantic.calls[0]["retrieved_contexts"] == ["父窗口"]
    assert list(id_metric.samples[0].retrieved_context_ids) == ["c1", "c2"]
    assert results[0]["scores"] == {
        "context_relevance": 0.75,
        "id_based_context_precision": 0.5,
    }
    assert results[0]["semantic_context_count"] == 1
    assert results[0]["retrieval_hit_count"] == 2


def test_score_samples_isolates_failures_and_nan():
    failing = FakeScoredMetric(error=RuntimeError("secret prompt"))
    nan_metric = FakeScoredMetric(math.nan)
    good = FakeScoredMetric(0.5)
    config = RagasConfig(metrics=("context_relevance", "faithfulness", "answer_relevancy"),
                         concurrency=2)
    results = asyncio.run(score_samples(
        [_sample()],
        {
            "context_relevance": failing,
            "faithfulness": nan_metric,
            "answer_relevancy": good,
        },
        config,
    ))
    row = results[0]
    assert row["scores"] == {"answer_relevancy": 0.5}
    assert row["errors"]["context_relevance"] == "METRIC_FAILED"
    assert row["errors"]["faithfulness"] == "METRIC_NAN"
    assert "secret" not in str(row)


def test_run_ragas_evaluation_async_returns_block_and_restores_logger():
    report = {
        "config": {"top_k": 5},
        "summary": {"precision": 0.2, "mrr": 0.5},
        "cases": [{
            "id": "1",
            "question": "q",
            "reference_answer": "参考回答",
            "relevant_chunk_ids": ["c1"],
            "evidence": [
                {"chunk_id": "c1", "parent_id": "p1", "text": "父窗口"},
                {"chunk_id": "c2", "parent_id": "p1", "text": "父窗口"},
            ],
            "answer": {"answer": "回答[c1]", "abstained": False},
        }],
    }
    metric = FakeScoredMetric(0.8)
    ragas_logger = logging.getLogger("ragas")
    old_propagate = ragas_logger.propagate
    block = asyncio.run(run_ragas_evaluation_async(
        report,
        RagasConfig(metrics=("context_relevance",)),
        llm=FakeLLM(),
        metrics={"context_relevance": metric},
    ))
    assert block["sample_count"] == 1
    assert block["scored_count"] == 1
    assert block["summary"]["context_relevance"] == 0.8
    assert block["valid_counts"]["context_relevance"] == 1
    assert block["cases"][0]["retrieval_hit_count"] == 2
    assert block["cases"][0]["semantic_context_count"] == 1
    assert metric.calls[0]["retrieved_contexts"] == ["父窗口"]
    assert ragas_logger.propagate == old_propagate
    assert os.environ.get("RAGAS_DO_NOT_TRACK") == "True"


class FlakyMetric:
    """Returns a scripted sequence and records how often it was called."""

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    async def ascore(self, **kwargs):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(value=value)


def test_repeat_sampling_takes_the_median():
    metric = FlakyMetric([0.2, 0.9, 0.5])
    config = RagasConfig(metrics=("context_relevance",), repeats=3)
    results = asyncio.run(score_samples(
        [_sample()], {"context_relevance": metric}, config))
    assert metric.calls == 3
    assert results[0]["scores"]["context_relevance"] == 0.5
    assert results[0]["repeats"] == {"context_relevance": 3}


def test_repeat_sampling_is_off_at_one_and_for_id_metrics():
    metric = FlakyMetric([0.4])
    config = RagasConfig(metrics=("context_relevance",), repeats=1)
    results = asyncio.run(score_samples(
        [_sample()], {"context_relevance": metric}, config))
    assert metric.calls == 1
    assert results[0]["scores"]["context_relevance"] == 0.4
    assert results[0]["repeats"] == {}

    id_metric = FakeIdMetric(0.5)
    config = RagasConfig(metrics=("id_based_context_precision",), repeats=3)
    asyncio.run(score_samples(
        [_sample()], {"id_based_context_precision": id_metric}, config))
    assert len(id_metric.samples) == 1


def test_repeat_sampling_tolerates_partial_failures():
    metric = FlakyMetric([RuntimeError("boom"), 0.6, 0.8])
    config = RagasConfig(metrics=("context_relevance",), repeats=3)
    results = asyncio.run(score_samples(
        [_sample()], {"context_relevance": metric}, config))
    assert results[0]["scores"]["context_relevance"] == 0.7
    assert "context_relevance" not in results[0]["errors"]


def test_repeat_sampling_reports_the_common_error_when_all_fail():
    metric = FlakyMetric([RuntimeError("boom")])
    config = RagasConfig(metrics=("context_relevance",), repeats=3)
    results = asyncio.run(score_samples(
        [_sample()], {"context_relevance": metric}, config))
    assert results[0]["errors"]["context_relevance"] == "METRIC_FAILED"
    assert "context_relevance" not in results[0]["scores"]
    assert "boom" not in str(results[0])


class TransientMetric:
    """Fails with a connection error a few times, then succeeds."""

    def __init__(self, failures, value=0.7):
        self.failures = failures
        self.value = value
        self.calls = 0

    async def ascore(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("connection reset")
        return SimpleNamespace(value=self.value)


def test_transient_error_is_retried_without_reaching_the_report(monkeypatch):
    from medical_rag import ragas_evaluation

    monkeypatch.setattr(ragas_evaluation, "_TRANSIENT_BACKOFF_SECONDS", 0.0)
    metric = TransientMetric(failures=2)
    config = RagasConfig(metrics=("context_relevance",), repeats=1)
    results = asyncio.run(score_samples(
        [_sample()], {"context_relevance": metric}, config))
    assert metric.calls == 3
    assert results[0]["scores"]["context_relevance"] == 0.7
    assert results[0]["errors"] == {}


def test_exhausted_transient_retries_report_the_stable_code(monkeypatch):
    from medical_rag import ragas_evaluation

    monkeypatch.setattr(ragas_evaluation, "_TRANSIENT_BACKOFF_SECONDS", 0.0)
    metric = TransientMetric(failures=99)
    config = RagasConfig(metrics=("context_relevance",), repeats=1)
    results = asyncio.run(score_samples(
        [_sample()], {"context_relevance": metric}, config))
    assert metric.calls == ragas_evaluation._TRANSIENT_ATTEMPTS
    assert results[0]["errors"]["context_relevance"] == "METRIC_FAILED"


def test_permanent_errors_are_not_retried(monkeypatch):
    from medical_rag import ragas_evaluation

    monkeypatch.setattr(ragas_evaluation, "_TRANSIENT_BACKOFF_SECONDS", 0.0)

    class Permanent:
        calls = 0

        async def ascore(self, **kwargs):
            Permanent.calls += 1
            raise ValueError("bad output shape")

    config = RagasConfig(metrics=("context_relevance",), repeats=1)
    results = asyncio.run(score_samples(
        [_sample()], {"context_relevance": Permanent()}, config))
    assert Permanent.calls == 1
    assert results[0]["errors"]["context_relevance"] == "METRIC_FAILED"


def test_api_error_codes_decide_retryability():
    from medical_rag import ragas_evaluation
    from medical_rag.types import ApiError

    assert ragas_evaluation._is_transient(ApiError("MODEL_UNAVAILABLE", "x", 503))
    assert ragas_evaluation._is_transient(ApiError("MODEL_RATE_LIMITED", "x", 429))
    assert not ragas_evaluation._is_transient(ApiError("MODEL_AUTH_FAILED", "x", 503))
    assert not ragas_evaluation._is_transient(ApiError("MODEL_NOT_CONFIGURED", "x", 503))


def test_run_ragas_evaluation_async_id_only_needs_no_llm():
    from medical_rag.ragas_evaluation import IDBasedContextPrecision

    report = {
        "cases": [{
            "id": "1",
            "question": "q",
            "relevant_chunk_ids": ["c1"],
            "evidence": [{"chunk_id": "c1", "text": "上下文"}],
        }]
    }
    block = asyncio.run(run_ragas_evaluation_async(
        report,
        RagasConfig(metrics=("id_based_context_precision",)),
        settings=None,
        llm=None,
        metrics={"id_based_context_precision": IDBasedContextPrecision()},
    ))
    assert block["summary"]["id_based_context_precision"] == 1.0
    assert block["scored_count"] == 1


def test_run_ragas_evaluation_async_marks_limit_skips_and_init_errors():
    report = {
        "cases": [
            {
                "id": "1",
                "question": "q",
                "evidence": [{"chunk_id": "c1", "text": "上下文"}],
                "answer": {"answer": "回答", "abstained": False},
            },
            {
                "id": "2",
                "question": "q2",
                "evidence": [{"chunk_id": "c2", "text": "上下文"}],
                "answer": {"answer": "回答", "abstained": False},
            },
        ]
    }
    block = asyncio.run(run_ragas_evaluation_async(
        report,
        RagasConfig(metrics=("faithfulness",), limit=1),
        llm=FakeLLM(),
        metrics={},
        init_errors={"faithfulness": "METRIC_INIT_FAILED"},
    ))
    assert block["cases"][0]["errors"]["faithfulness"] == "METRIC_INIT_FAILED"
    assert block["cases"][1]["skipped"]["faithfulness"] == "LIMIT_SKIPPED"
    assert block["failure_counts"]["METRIC_INIT_FAILED"] == 1
    assert block["skip_counts"]["LIMIT_SKIPPED"] == 1
