"""Lazy local BGE embedding and reranking adapters."""

import math


class BgeEmbedder:
    def __init__(self, model: object | None = None) -> None:
        self._model = model

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        vectors = self._model.encode(texts, normalize_embeddings=True)
        result = [[float(value) for value in row] for row in vectors]
        if len(result) != len(texts) or any(len(row) != 512 or not all(math.isfinite(x) for x in row) for row in result):
            raise ValueError("向量输出必须与输入对应，且为 512 维有限数值")
        return result


class BgeReranker:
    def __init__(self, model: object | None = None) -> None:
        self._model = model

    def score(self, question: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder("BAAI/bge-reranker-v2-m3")
        logits = self._model.predict([[question, text] for text in texts])
        scores = [1.0 / (1.0 + math.exp(-float(value))) for value in logits]
        if len(scores) != len(texts) or not all(math.isfinite(score) for score in scores):
            raise ValueError("重排分数无效")
        return scores
