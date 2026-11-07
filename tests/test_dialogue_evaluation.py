"""Fixed synthetic evaluation: contract checks and de-identified metrics."""

import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from medical_rag.dialogue_evaluation import (
    CASE_COUNT,
    CaseError,
    evaluate_cases,
    load_cases,
    main,
    replay_factory,
    score_turn,
    validate_cases,
)

CASES = Path(__file__).resolve().parents[1] / "data/evaluations/dialogue-followups-v1/cases.json"


def test_scoring_requires_original_constraints_and_clarification():
    expected = {"topic": "糖尿病", "intent": "etiology", "must_clarify": False,
                "required_terms": ["糖尿病"], "intent_terms": ["病因", "原因", "引起"],
                "forbidden_terms": ["高血压", "2型"]}
    good = {"status": "resolved", "topic": "糖尿病", "intent": "etiology",
            "question": "糖尿病的病因是什么？"}
    drift = {**good, "question": "2型糖尿病的病因是什么？"}
    assert score_turn(expected, good)["question_ok"] is True
    assert score_turn(expected, drift)["question_ok"] is False
    assert score_turn({**expected, "must_clarify": True}, good)["clarification_ok"] is False


def test_the_fixed_case_file_is_valid():
    cases = load_cases(CASES)
    assert len(cases) == CASE_COUNT
    assert len({case["id"] for case in cases}) == CASE_COUNT


@pytest.mark.parametrize("mutate", [
    lambda cases: cases[:-1],
    lambda cases: cases + [copy.deepcopy(cases[0])],
    lambda cases: [cases[0].copy() | {"turns": cases[0]["turns"][:2]}],
    lambda cases: [cases[0].copy() | {"turns": cases[0]["turns"] * 2}],
    lambda cases: [cases[0], cases[0]] + cases[2:],
    lambda cases: [case.copy() | {"turns": [
        turn.copy() | {"must_clarify": False} for turn in case["turns"]]}
        for case in cases],
])
def test_structural_violations_are_rejected(mutate):
    cases = copy.deepcopy(load_cases(CASES))
    with pytest.raises(CaseError):
        validate_cases(mutate(cases))


def test_a_missing_turn_field_is_rejected():
    cases = copy.deepcopy(load_cases(CASES))
    del cases[0]["turns"][0]["forbidden_terms"]
    with pytest.raises(CaseError):
        validate_cases(cases)


def test_live_mode_refuses_before_building_any_model(monkeypatch):
    def forbidden():
        raise AssertionError("a paid model must not be constructed without opt-in")

    monkeypatch.setattr("medical_rag.dialogue_evaluation.live_factory", forbidden)
    assert main(["--cases", str(CASES), "--mode", "live"]) == 2


def test_replay_mode_runs_the_fixed_cases_and_reports_no_case_text():
    output = io.StringIO()
    with redirect_stdout(output):
        assert main(["--cases", str(CASES), "--mode", "replay"]) == 0
    summary = json.loads(output.getvalue())
    assert summary["cases"] == CASE_COUNT
    assert summary["clarification_turns"] == 10
    assert "latency_ms_p50" in summary and "latency_ms_p95" in summary
    assert "results" not in summary
    cases = load_cases(CASES)
    for question in (turn["question"] for case in cases for turn in case["turns"]):
        assert question not in output.getvalue()


class FixedResolver:
    """Replay replacement that always reports the expected shape."""

    transport = None

    def resolve(self, question, snapshot):
        from medical_rag.dialogue_models import Resolution

        return Resolution.model_validate_json(json.dumps({
            "original_question": question, "status": "clarification",
            "relation": "follow_up", "resolved_question": None, "topic": None,
            "intent": "other", "intent_text": "", "references": [],
            "clarification": None,
        }, ensure_ascii=False))


def test_metrics_separate_clarification_turns_from_the_topic_and_intent_denominator():
    cases = load_cases(CASES)
    summary = evaluate_cases(cases, replay_factory())
    assert summary["clarification_turns"] == 10
    assert summary["topic_accuracy"] >= 0.95
    assert summary["intent_accuracy"] >= 0.95
    assert summary["clarification_accuracy"] == 1.0
    # 40 组、每组至少 3 轮；每个非歧义轮次都进入 topic/intent 分母。
    assert summary["turns"] == sum(len(case["turns"]) for case in cases)

    failed = evaluate_cases(cases, lambda case: FixedResolver())
    # 一律澄清的解析器：主题/意图全错，澄清轮次全对。两个指标必须分开报告，
    # 不能用一个总平均把“总是要求补充”算成及格。
    assert failed["topic_accuracy"] == 0.0
    assert failed["intent_accuracy"] == 0.0
    assert failed["clarification_accuracy"] == 1.0
    assert failed["clarification_turns"] == 10
