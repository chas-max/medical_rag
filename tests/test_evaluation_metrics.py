import pytest

from medical_rag.evaluation_metrics import (
    cited_retrieval_scores,
    mean_or_none,
    parent_window_scores,
    retrieval_scores,
)


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


def test_graded_metrics_reward_core_chunks_and_top_ranks():
    grades = {"core1": 2, "core2": 2, "supp1": 1}
    perfect = retrieval_scores(
        ["core1", "core2", "supp1", "x"],
        set(grades),
        4,
        chunk_grades=grades,
    )
    assert perfect["ndcg"] == pytest.approx(1.0)
    assert perfect["core_recall"] == pytest.approx(1.0)
    assert perfect["hit_rate"] == 1.0

    suboptimal = retrieval_scores(
        ["supp1", "core1", "x"],
        set(grades),
        3,
        chunk_grades=grades,
    )
    assert suboptimal["recall"] == pytest.approx(2 / 3)
    assert suboptimal["core_recall"] == pytest.approx(0.5)
    assert 0 < suboptimal["ndcg"] < 1.0
    assert suboptimal["hit_rate"] == 1.0


def test_graded_metrics_fallback_safely_without_grades_or_hits():
    score = retrieval_scores(["x", "y"], {"a", "b"}, 2)
    assert score["ndcg"] == 0.0
    assert score["core_recall"] == 0.0
    assert score["hit_rate"] == 0.0

    only_supp = retrieval_scores(["s1"], {"s1", "s2"}, 2, chunk_grades={"s1": 1, "s2": 1})
    assert only_supp["core_recall"] == pytest.approx(0.5)


def test_parent_window_scores_dedups_parents_and_computes_r_precision():
    from types import SimpleNamespace

    def make_ev(cid, parent_id, text):
        return SimpleNamespace(chunk=SimpleNamespace(id=cid, parent_id=parent_id, context_text=text, text=text))

    ev_list = [
        make_ev("c1_a", "p1", "证据一句"),
        make_ev("c1_b", "p1", "同一父窗不重复计数"),
        make_ev("c2_a", "p2", "噪声片段"),
        make_ev("c3_a", "p3", "证据二句"),
    ]
    res = parent_window_scores(
        ev_list,
        {"expected_other_child"},
        k=3,
        source_refs=[
            {"quote": "证据一句", "relevance": 2},
            {"quote": "证据二句", "relevance": 1},
        ],
    )
    # 3 distinct parent windows (p1, p2, p3); first R=2 positions contain 1 hit (p1) -> r_precision=0.5
    assert res["retrieved_count"] == 3
    assert res["recall"] == pytest.approx(1.0)
    assert res["core_recall"] == pytest.approx(1.0)
    assert res["precision"] == pytest.approx(2 / 3)
    assert res["r_precision"] == pytest.approx(0.5)

    cited = cited_retrieval_scores(
        ["c1_a"],
        ev_list,
        {"c1_a", "c3_a"},
        source_refs=[
            {"quote": "证据一句", "relevance": 2},
            {"quote": "证据二句", "relevance": 1},
        ],
    )
    assert cited["precision"] == pytest.approx(1.0)
    assert cited["recall"] == pytest.approx(0.5)
    assert cited["cited_count"] == 1
    empty = cited_retrieval_scores([], ev_list, {"c1_a"})
    assert empty["cited_count"] == 0 and empty["precision"] == 0.0
