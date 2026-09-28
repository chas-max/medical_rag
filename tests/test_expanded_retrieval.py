import pytest

from medical_rag.types import Chunk, Evidence
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.nodes.retrieval import expanded


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


def test_expanded_retrieval_fuses_duplicates_and_uses_hyde_only_for_dense():
    store = Store()
    result = expanded({"query_plan": {"questions": ["原问题", "抽象问题"],
                                       "hypothetical": "模拟答案"}},
                      {"k": 60}, ExecutionContext("run", "w", {}), store, Embedder(), 20)
    assert len(result["evidence"]) == 1
    assert result["evidence"][0].fused_score == 5 / 61
    assert [call[0] for call in store.bm25_calls] == ["原问题", "抽象问题"]
    assert len(store.dense_calls) == 3
    assert all(call[1] == 20 for call in store.dense_calls)


def test_expanded_retrieval_rejects_bad_rrf_k():
    with pytest.raises(ValueError, match="RRF k"):
        expanded({"query_plan": {"questions": ["原问题"], "hypothetical": None}},
                 {"k": 0}, ExecutionContext("run", "w", {}), Store(), Embedder(), 20)
