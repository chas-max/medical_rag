"""Strict bounded model contracts, independent of model and storage adapters."""

import pytest
from pydantic import ValidationError


def test_composable_intents_and_strict_flags():
    from medical_rag.profile_models import IntentDecision

    decision = IntentDecision.model_validate({"needs_answer": True, "lookup_keys": ["occupation"]})
    assert decision.needs_answer and decision.lookup_keys == ["occupation"]
    for invalid in ({"needs_answer": "false"}, {"needs_answer": False, "user_id": "other"},
                    {"needs_answer": False, "lookup_keys": ["unknown"]}):
        with pytest.raises(ValidationError):
            IntentDecision.model_validate(invalid)


def test_candidates_are_bounded():
    from medical_rag.profile_models import Candidate, IntentDecision

    valid = {"key": "occupation", "operation": "assert", "value": "护士",
             "support": "我是一名护士", "subject": "user", "modality": "positive",
             "sensitivity": "normal"}
    assert Candidate.model_validate(valid).value == "护士"
    for changes in ({"support": ""}, {"value": "x" * 501}, {"subject": "inferred"},
                    {"sensitivity": False}, {"sql": "DELETE"}):
        with pytest.raises(ValidationError):
            Candidate.model_validate({**valid, **changes})
    with pytest.raises(ValidationError):
        IntentDecision.model_validate({"needs_answer": False, "candidates": [valid] * 13})
