import pytest

from medical_rag.evaluation_metrics import mean_or_none, retrieval_scores


def test_ranked_metrics_use_relevant_count_and_position():
    score = retrieval_scores(["x", "a", "b"], {"a", "b", "c"}, 3)
    assert score["precision"] == pytest.approx(2 / 3)
    assert score["recall"] == pytest.approx(2 / 3)
    assert score["f1"] == pytest.approx(2 / 3)
    assert score["ap"] == pytest.approx((1 / 2 + 2 / 3) / 3)
    assert score["rr"] == 0.5
    assert score["hit_ids"] == ["a", "b"]


def test_empty_and_duplicate_results():
    assert retrieval_scores([], {"a"}, 5)["f1"] == 0
    score = retrieval_scores(["a", "a", "b"], {"a", "b"}, 3)
    assert score["retrieved_count"] == 2
    assert score["ap"] == 1


def test_k_caps_ranking_and_invalid_inputs():
    assert retrieval_scores(["x", "a"], {"a"}, 1)["rr"] == 0
    with pytest.raises(ValueError):
        retrieval_scores([], {"a"}, 0)
    with pytest.raises(ValueError):
        retrieval_scores([], set(), 1)
    assert mean_or_none([]) is None
    assert mean_or_none([0.5, 1]) == 0.75
