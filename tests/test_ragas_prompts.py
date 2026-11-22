"""Tests for the Chinese prompt overrides.

These import Ragas, so the whole module is skipped when the optional evaluation
dependency is not installed.
"""

import pytest

pytest.importorskip("ragas")

from medical_rag.ragas_adapter import RagasConfig  # noqa: E402
from medical_rag.ragas_evaluation import build_metrics  # noqa: E402
from medical_rag.ragas_llm import BgeRagasEmbedding, QwenRagasLLM  # noqa: E402
from medical_rag.ragas_prompts import (  # noqa: E402
    OVERRIDE_ATTRIBUTES,
    PROMPT_OVERRIDES,
    apply_chinese_prompts,
)


class FakeTransport:
    """Only needs to exist; these tests never issue a model call."""

    def complete(self, provider, system, user):
        raise AssertionError("no model call expected")


class FakeEmbedder:
    def encode(self, texts):
        return [[0.0, 1.0] for _ in texts]


def _llm():
    return QwenRagasLLM(FakeTransport(), model="qwen3.8-flash")


def _embeddings():
    return BgeRagasEmbedding(FakeEmbedder())


def _instructions(metric):
    """Every prompt attribute that carries an instruction string."""
    found = {}
    for attribute in ("prompt", "statement_generator_prompt", "nli_statement_prompt"):
        prompt = getattr(metric, attribute, None)
        instruction = getattr(prompt, "instruction", None)
        if isinstance(instruction, str):
            found[attribute] = instruction
    return found


def test_all_expected_metrics_have_an_override():
    assert set(PROMPT_OVERRIDES) == {
        "answer_relevancy",
        "context_precision",
        "context_recall",
        "faithfulness",
    }
    assert OVERRIDE_ATTRIBUTES["faithfulness"] == (
        "statement_generator_prompt",
        "nli_statement_prompt",
    )


def test_build_metrics_applies_chinese_prompts_by_default():
    config = RagasConfig(
        metrics=("answer_relevancy", "context_recall", "context_precision", "faithfulness"),
        chinese_prompts=True,
    )
    metrics, errors = build_metrics(_llm(), _embeddings(), config)
    assert errors == {}
    instructions = _instructions(metrics["answer_relevancy"])
    assert "Simplified Chinese" in instructions["prompt"]
    assert "Chinese sentence punctuation" in _instructions(metrics["context_recall"])["prompt"]
    faithfulness = _instructions(metrics["faithfulness"])
    assert "Simplified Chinese" in faithfulness["statement_generator_prompt"]
    assert "Simplified Chinese" in faithfulness["nli_statement_prompt"]
    assert "Simplified Chinese" in _instructions(metrics["context_precision"])["prompt"]


def test_build_metrics_can_keep_upstream_english_prompts():
    config = RagasConfig(metrics=("answer_relevancy",), chinese_prompts=False)
    metrics, _ = build_metrics(_llm(), _embeddings(), config)
    instruction = _instructions(metrics["answer_relevancy"])["prompt"]
    assert "Simplified Chinese" not in instruction
    # The override class must not be attached at all in this mode.
    assert type(metrics["answer_relevancy"].prompt).__name__ == "AnswerRelevancePrompt"


def test_apply_ignores_metrics_that_are_absent():
    assert apply_chinese_prompts({}) == {}


def test_apply_is_idempotent_and_reports_attributes():
    config = RagasConfig(metrics=("answer_relevancy", "faithfulness"), chinese_prompts=True)
    metrics, _ = build_metrics(_llm(), _embeddings(), config)
    first = apply_chinese_prompts(metrics)
    second = apply_chinese_prompts(metrics)
    assert first == second
    assert first["answer_relevancy"] == "prompt"
    assert first["faithfulness"] in ("statement_generator_prompt", "nli_statement_prompt")


def test_report_config_records_the_switch():
    assert RagasConfig().to_report_config(model="m")["chinese_prompts"] is True
    assert RagasConfig(chinese_prompts=False).to_report_config(model="m")["chinese_prompts"] is False
