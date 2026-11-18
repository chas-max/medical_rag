import pytest

from medical_rag.types import Chunk, Evidence
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.nodes.retrieval import multi


class Store:
    def __init__(self):
        self.dense_calls = []
        self.bm25_calls = []
        self.chunk = Chunk("c1", "d", "guide.md", "随访原则", "guide.md", "", None, 0)

    def search_dense(self, vector, limit):
        self.dense_calls.append((vector, limit))
        return [Evidence(self.chunk, 0.9)]

    def search_bm25(self, text, limit):
        self.bm25_calls.append((text, limit))
        return [Evidence(self.chunk, 0.8)]


class Embedder:
    def encode(self, texts):
        return [[len(texts[0])]]


def run(queries, config=None, strategy=None):
    store, embedder = Store(), Embedder()
    handler_config = {"k": 60} if config is None else config
    if strategy is not None:
        handler_config = {**handler_config, "strategy": strategy}
    result = multi({"queries": queries}, handler_config, ExecutionContext("run", "w", {}),
                   store, embedder, 20)
    return result, store


def test_multi_retrieval_searches_every_connected_query_and_fuses():
    """连线数就是检索路数：两个问题各走 dense+bm25，融合成一份证据。"""
    result, store = run(["原问题", "抽象问题"])
    assert len(result["evidence"]) == 1
    assert result["evidence"][0].fused_score == 4 / 61
    assert [call[0] for call in store.bm25_calls] == ["原问题", "抽象问题"]
    assert len(store.dense_calls) == 2
    assert all(call[1] == 20 for call in store.dense_calls)


@pytest.mark.parametrize("strategy,dense,bm25", [("dense", 1, 0), ("bm25", 0, 1)])
def test_multi_retrieval_strategy_narrows_the_paths(strategy, dense, bm25):
    _, store = run(["原问题"], strategy=strategy)
    assert len(store.dense_calls) == dense
    assert len(store.bm25_calls) == bm25


def test_multi_retrieval_rejects_bad_rrf_k():
    with pytest.raises(ValueError, match="RRF k"):
        run(["原问题"], config={"k": 0})


def test_multi_retrieval_needs_at_least_one_query_path():
    with pytest.raises(ValueError, match="检索策略"):
        run([], config={"k": 60, "strategy": "unknown"})



def test_multi_retrieval_primary_boost_and_top_k():
    class DualStore:
        def __init__(self):
            self.dense_calls = []
            self.bm25_calls = []

        def search_dense(self, vector, limit):
            self.dense_calls.append(limit)
            cid = "c_primary" if vector[0] == 3 else "c_sub"
            return [Evidence(Chunk(cid, "d", "g.md", cid, "g.md", "", None, 0), 0.9)]

        def search_bm25(self, text, limit):
            self.bm25_calls.append((text, limit))
            cid = "c_primary" if text == "主问题" else "c_sub"
            return [Evidence(Chunk(cid, "d", "g.md", cid, "g.md", "", None, 0), 0.9)]

    store = DualStore()
    result = multi(
        {"queries": ["主问题", "子问B", "主问题"]},
        {"k": 60, "top_k": 30, "primary_boost": 1.5},
        ExecutionContext("run", "w", {}),
        store,
        Embedder(),
        20,
    )
    assert store.dense_calls == [30, 30]
    assert [c[0] for c in store.bm25_calls] == ["主问题", "子问B"]
    assert [item.chunk.id for item in result["evidence"]] == ["c_primary", "c_sub"]


def test_rerank_respects_max_per_parent_and_backfills():
    from medical_rag.workflow.nodes.retrieval import rerank

    class FakeReranker:
        def score_with_kind(self, question, texts):
            return [0.95 - idx * 0.05 for idx in range(len(texts))], "rerank"

    chunks = [
        Chunk("p1_a", "d", "g.md", "a", "g.md", "", None, 0, parent_id="p1", parent_text="P1"),
        Chunk("p1_b", "d", "g.md", "b", "g.md", "", None, 1, parent_id="p1", parent_text="P1"),
        Chunk("p1_c", "d", "g.md", "c", "g.md", "", None, 2, parent_id="p1", parent_text="P1"),
        Chunk("p1_d", "d", "g.md", "d", "g.md", "", None, 3, parent_id="p1", parent_text="P1"),
        Chunk("p2_a", "d", "g.md", "e", "g.md", "", None, 4, parent_id="p2", parent_text="P2"),
        Chunk("p3_a", "d", "g.md", "f", "g.md", "", None, 5, parent_id="p3", parent_text="P3"),
    ]
    pool = [Evidence(ch, 0.5) for ch in chunks]
    diverse = rerank(
        {"question": "q", "evidence": pool},
        {"top_k": 4, "max_per_parent": 2, "backfill_same_parent": True},
        ExecutionContext("run", "w", {}),
        FakeReranker(),
        0.5,
        15,
        5,
    )["evidence"]
    assert [item.chunk.id for item in diverse] == ["p1_a", "p1_b", "p2_a", "p3_a"]

    backfilled = rerank(
        {"question": "q", "evidence": pool[:3]},
        {"top_k": 3, "max_per_parent": 1, "backfill_same_parent": True},
        ExecutionContext("run", "w", {}),
        FakeReranker(),
        0.5,
        15,
        5,
    )["evidence"]
    assert [item.chunk.id for item in backfilled] == ["p1_a", "p1_b", "p1_c"]


def test_rrf_weights_secondary_sources_lower():
    from medical_rag.workflow.nodes.retrieval import rrf

    primary = [Evidence(Chunk("p1", "d", "g.md", "p1", "g.md", "", None, 0), 0.8)]
    secondary = [Evidence(Chunk("h1", "d", "g.md", "h1", "g.md", "", None, 1), 0.9)]
    fused = rrf(
        {"sources": [primary], "secondary_sources": [secondary]},
        {"k": 60, "primary_weight": 1.0, "secondary_weight": 0.35},
        ExecutionContext("run", "w", {}),
    )["evidence"]
    assert [item.chunk.id for item in fused] == ["p1", "h1"]
    assert fused[0].fused_score > fused[1].fused_score


def test_multi_applies_filename_boost_qa_dampen_and_boilerplate_penalty():
    c_official = Chunk("c_off", "d1", "中国高血压防治指南(2024年修订版).pdf", "随访管理目标与降压药选择", "指南.pdf", "治疗", 1, 0)
    c_qa = Chunk("c_qa", "d2", "中国高血压防治指南（患者问答）.pdf", "随访管理目标与降压药选择", "问答.pdf", "治疗", 1, 1)
    c_ref = Chunk("c_ref", "d3", "中国高血压防治指南(2024年修订版).pdf", "1. Author et al. J Hypertens 2020;38:1-10. [2] Smith J. 2021;12:3.", "指南.pdf", "参考文献", 1, 2)

    class PriorStore:
        def search_dense(self, vector, limit):
            return [Evidence(c_ref, 0.95), Evidence(c_qa, 0.90), Evidence(c_official, 0.85)]

        def search_bm25(self, text, limit):
            return [Evidence(c_ref, 0.95), Evidence(c_qa, 0.90), Evidence(c_official, 0.85)]

    res = multi(
        {"queries": ["《中国高血压防治指南》的随访目标是什么？"]},
        {
            "k": 40,
            "top_k": 10,
            "filename_boost_weight": 0.45,
            "qa_dampen_factor": 0.72,
            "boilerplate_penalty": 0.25,
        },
        ExecutionContext("run", "w", {}),
        PriorStore(),
        Embedder(),
        20,
    )
    assert [item.chunk.id for item in res["evidence"]] == ["c_off", "c_qa", "c_ref"]


def test_rerank_subquery_quota_preserves_calibrated_score_without_rrf_pollution():
    from medical_rag.types import SCORE_RERANK
    from medical_rag.workflow.nodes.retrieval import rerank

    class SubqueryReranker:
        def __init__(self):
            self.calls = []

        def score_with_kind(self, query, texts):
            self.calls.append((query, list(texts)))
            if "子问B" in query:
                return [0.40, 0.92, 0.50], SCORE_RERANK
            return [0.85, 0.55, 0.60], SCORE_RERANK

    c1 = Chunk("c1", "d", "g.pdf", "正文A", "g.pdf", "第1章", 1, 0, parent_id="p1")
    c2 = Chunk("c2", "d", "g.pdf", "正文B", "g.pdf", "第2章", 2, 1, parent_id="p2")
    c3 = Chunk("c3", "d", "g.pdf", "正文C", "g.pdf", "第3章", 3, 2, parent_id="p3")
    pool = [
        Evidence(c1, 0.01, fused_score=0.01),
        Evidence(c2, 0.02, fused_score=0.02),
        Evidence(c3, 0.05, fused_score=0.05),
    ]
    rr = SubqueryReranker()
    result = rerank(
        {"question": "主问题", "evidence": pool, "subqueries": ["子问B"]},
        {
            "top_k": 2,
            "include_metadata": True,
            "subquery_blend_weight": 0.35,
            "rrf_blend_weight": 0.15,
            "subquery_quota": 1,
            "max_per_parent": 1,
        },
        ExecutionContext("run", "w", {}),
        rr,
        0.5,
        15,
        5,
    )["evidence"]
    # include_metadata forwards titled strings to reranker while chunk.text stays clean
    assert rr.calls[0][1][0].startswith("《g》 (第1章)\n正文A")
    ids = [item.chunk.id for item in result]
    assert "c2" in ids and "c1" in ids
    # Evidence.score reflects pure calibrated s_agg without rrf_blend_weight added to it
    c1_item = next(item for item in result if item.chunk.id == "c1")
    assert c1_item.score == pytest.approx(0.85)
