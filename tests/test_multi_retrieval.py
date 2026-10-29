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
