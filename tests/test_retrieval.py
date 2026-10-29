import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.retrieval import Retriever
from medical_rag.types import Chunk, Evidence


def evidence(block_id, text):
    return Evidence(Chunk(block_id, "doc", "source", text, "guide.md", "管理", None, 0), 0.8)


class Store:
    def __init__(self):
        self.dense = [evidence("a", "血压监测"), evidence("b", "饮食管理")]
        self.sparse = [evidence("b", "饮食管理"), evidence("c", "药物治疗")]

    def search_dense(self, vector, limit):
        return self.dense

    def search_bm25(self, question, limit):
        return self.sparse


class Embedder:
    def encode(self, texts):
        return [[1.0] * 512]


class Reranker:
    def score(self, question, texts):
        return [0.9 if text == "饮食管理" else 0.6 for text in texts]


def test_rrf_deduplicates_and_reranks_candidates():
    result = Retriever(Store(), Embedder(), Reranker(), candidate_k=3, threshold=0.5).search("管理", 3)
    assert [item.chunk.id for item in result] == ["b", "a", "c"]
    assert result[0].fused_score == 1 / 62 + 1 / 61
    assert result[0].score == 0.9


def test_low_scoring_evidence_is_filtered():
    result = Retriever(Store(), Embedder(), Reranker(), candidate_k=3, threshold=0.95).search("管理", 3)
    assert result == []
