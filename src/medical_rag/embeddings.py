"""Lazy local BGE embedding and reranking adapters."""

import logging
import math
import re


logger = logging.getLogger(__name__)

EMBEDDER_MODEL = "BAAI/bge-small-zh-v1.5"
RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
# The reranker advertises an 8192-token window; feeding it whole chunks costs ~40s per
# 35-candidate pool on CPU, so cap it at the embedder's 512-token context.
RERANKER_MAX_LENGTH = 512
# fp32 weights (2.3 GB) get evicted and paged on a 16 GB CPU box; bfloat16 halves the
# footprint and costs nothing measurable in ranking (0 inversions over 15 candidates).
RERANKER_DTYPE = "bfloat16"


class BgeEmbedder:
    def __init__(self, model: object | None = None) -> None:
        self._model = model

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(EMBEDDER_MODEL, local_files_only=True)
        vectors = self._model.encode(texts, normalize_embeddings=True)
        result = [[float(value) for value in row] for row in vectors]
        if len(result) != len(texts) or any(len(row) != 512 or not all(math.isfinite(x) for x in row) for row in result):
            raise ValueError("向量输出必须与输入对应，且为 512 维有限数值")
        return result


class BgeReranker:
    def __init__(self, model: object | None = None) -> None:
        self._model = model
        self._model_unavailable = False

    def score(self, question: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        if self._model_unavailable:
            return self._fallback_scores(question, texts)

        try:
            if self._model is None:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(RERANKER_MODEL, local_files_only=True,
                                           max_length=RERANKER_MAX_LENGTH,
                                           model_kwargs={"torch_dtype": RERANKER_DTYPE})
            logits = self._model.predict([[question, text] for text in texts])
            scores = [1.0 / (1.0 + math.exp(-float(value))) for value in logits]
            if len(scores) != len(texts) or not all(math.isfinite(score) for score in scores):
                raise ValueError("重排分数无效")
            return scores
        except Exception as error:
            self._model_unavailable = True
            logger.warning("重排模型不可用，切换到本地词项重排: %s", error)
            return self._fallback_scores(question, texts)

    @staticmethod
    def _fallback_scores(question: str, texts: list[str]) -> list[float]:
        """Score candidates without a downloaded cross-encoder model.

        This keeps retrieval available in offline deployments. Candidates with
        no lexical overlap stay below the default evidence threshold; matches
        receive a deterministic boost for downstream evidence gating.
        """

        query_tokens = set(_tokenize(question))
        if not query_tokens:
            return [0.0 for _ in texts]
        scores: list[float] = []
        for text in texts:
            text_tokens = set(_tokenize(text))
            overlap = len(query_tokens & text_tokens) / len(query_tokens)
            scores.append(0.5 + 0.5 * overlap if overlap else 0.0)
        return scores


def _tokenize(text: str) -> list[str]:
    """Return words and CJK characters for dependency-free lexical scoring."""

    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.casefold())
