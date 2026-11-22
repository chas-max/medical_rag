"""Chinese-language prompt variants for Ragas metrics on a Chinese corpus.

Ragas ships English instructions and has no language constraint, so
``qwen3.8-flash`` sometimes answers a Chinese answer with an *English* generated
question. The comparison embedding (``BAAI/bge-small-zh-v1.5``) is a Chinese model,
so a semantically-equivalent English question only scores ~0.41 cosine similarity
against a Chinese question, while a Chinese paraphrase scores ~1.0.

Measured on 50 questions (medical-health-v2):

* default prompt: 35.3% of generated questions were English; mean score 0.6980,
  stdev 0.1632
* Chinese prompt: 0% English; mean score 0.8175, stdev 0.0626

The ordering still works: good answer 0.883 > other-topic answer 0.571 >
noncommittal 0.394, and ``noncommittal`` detection still fires.

Only the *instruction* is rewritten; every JSON Schema, example set and output
model is inherited unchanged, so the metric's scoring code is untouched.
"""

from __future__ import annotations

# Shared suffix: tell the model to stay in Chinese but keep the structured contract.
_SAME_LANGUAGE = (
    " IMPORTANT: the answer, context and reference are all written in Simplified Chinese. "
    "Produce all free-text fields in Simplified Chinese as well; do NOT translate the "
    "source text into English."
)


def _build_prompt_classes() -> dict[str, tuple[type, type]]:
    """Build prompt subclasses once, defensively.

    Importing Ragas metric internals is version-sensitive, so a missing module
    disables only that metric's prompt override rather than the whole run.
    """
    classes: dict[str, tuple[type, type]] = {}

    try:
        from ragas.metrics.collections.answer_relevancy.util import AnswerRelevancePrompt

        class ChineseAnswerRelevancePrompt(AnswerRelevancePrompt):
            instruction = (
                "Generate a question for the given answer and identify if the answer is "
                "noncommittal. IMPORTANT: the generated question MUST be written in "
                "Simplified Chinese, the same language as the answer. Never translate the "
                "answer into English. Give noncommittal as 1 if the answer is noncommittal "
                "(evasive, vague, or ambiguous) and 0 if the answer is substantive. "
                'Examples of noncommittal answers: "I don\'t know", "I\'m not sure", '
                '"It depends".'
            )

        classes["answer_relevancy"] = ("prompt", ChineseAnswerRelevancePrompt)
    except Exception:  # noqa: BLE001 - optional override
        pass

    try:
        from ragas.metrics.collections.context_recall.util import ContextRecallPrompt

        class ChineseContextRecallPrompt(ContextRecallPrompt):
            instruction = (
                "Given a question, a context, and a reference answer, analyze each sentence "
                "in the reference answer and determine whether the sentence can be attributed "
                "to the context. Split the reference answer by Chinese sentence punctuation "
                "(。；！？) and analyze each Chinese sentence directly; do NOT translate it "
                "into English first, and do not merge separate sentences into one statement. "
                "Return the classifications in the same order as the reference sentences."
            )

        classes["context_recall"] = ("prompt", ChineseContextRecallPrompt)
    except Exception:  # noqa: BLE001 - optional override
        pass

    try:
        from ragas.metrics.collections.faithfulness.util import (
            NLIStatementPrompt,
            StatementGeneratorPrompt,
        )

        class ChineseStatementGeneratorPrompt(StatementGeneratorPrompt):
            instruction = (
                "Given a question and an answer in Simplified Chinese, analyze the "
                "complexity of each sentence in the answer. Break down each sentence into "
                "one or more fully understandable statements, keeping the original Chinese "
                "wording and meaning. Ensure that no pronouns are used in any statement."
            )

        class ChineseNLIStatementPrompt(NLIStatementPrompt):
            instruction = (
                "Your task is to judge the faithfulness of a series of statements based on a "
                "given context. The statements and the context are in Simplified Chinese; "
                "reason about them in Chinese. For each statement you must return verdict as "
                "1 if the statement can be directly inferred based on the context or 0 if the "
                "statement can not be directly inferred based on the context."
            )

        classes["faithfulness"] = (
            ("statement_generator_prompt", ChineseStatementGeneratorPrompt),
            ("nli_statement_prompt", ChineseNLIStatementPrompt),
        )
    except Exception:  # noqa: BLE001 - optional override
        pass

    try:
        from ragas.metrics.collections.context_precision.util import ContextPrecisionPrompt

        class ChineseContextPrecisionPrompt(ContextPrecisionPrompt):
            instruction = (
                "Given a question, a reference answer and a context, verify whether the "
                "context was useful in arriving at the given answer. The question, answer and "
                "context are in Simplified Chinese; judge them directly in Chinese. Give "
                "verdict as 1 if useful and 0 if not, with JSON output."
            )

        classes["context_precision"] = ("prompt", ChineseContextPrecisionPrompt)
    except Exception:  # noqa: BLE001 - optional override
        pass

    return classes


# metric name -> (attribute name, prompt class) or tuple of such pairs
PROMPT_OVERRIDES: dict[str, object] = _build_prompt_classes()

# Kept for documentation and diagnostics; the values are attribute names.
OVERRIDE_ATTRIBUTES = {
    name: (spec[0] if isinstance(spec, tuple) and not isinstance(spec[0], tuple)
           else tuple(item[0] for item in spec))
    for name, spec in PROMPT_OVERRIDES.items()
}


def apply_chinese_prompts(metrics: dict[str, object]) -> dict[str, str]:
    """Swap in the Chinese prompt classes for the metrics present.

    Returns a sanitized map of metric -> attribute so the report can record which
    prompts actually took effect. Failures are omitted rather than raised: a
    prompt override is an accuracy improvement, not a correctness requirement.
    """
    applied: dict[str, str] = {}
    for name, spec in PROMPT_OVERRIDES.items():
        metric = metrics.get(name)
        if metric is None:
            continue
        pairs = spec if isinstance(spec, tuple) and spec and isinstance(spec[0], tuple) else (spec,)
        for attribute, prompt_class in pairs:  # type: ignore[misc]
            try:
                setattr(metric, attribute, prompt_class())
                applied[name] = attribute
            except Exception:  # noqa: BLE001 - keep the default English prompt
                continue
    return applied
