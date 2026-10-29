"""Lazy local BGE embedding and reranking adapters."""

import logging
import math
import os
from time import monotonic

from .lexical import score as lexical_score
from .types import SCORE_LEXICAL, SCORE_RERANK


logger = logging.getLogger(__name__)

EMBEDDER_MODEL = "BAAI/bge-small-zh-v1.5"
RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
# The reranker advertises an 8192-token window; feeding it whole chunks costs ~40s per
# 35-candidate pool on CPU, so cap it at the embedder's 512-token context.
RERANKER_MAX_LENGTH = 512
# bfloat16 halves the reranker's weight footprint (~2.3 GB fp32 → ~1.2 GB), which fits
# the 8 GB laptop GPU next to the embedder, and costs nothing measurable in ranking
# (0 inversions over 15 candidates vs fp32).
RERANKER_DTYPE = "bfloat16"
# Optional pin: RAG_MODEL_DEVICE=cpu|cuda|cuda:0. Default auto-detects CUDA and only
# falls back to CPU when the torch build has no usable GPU (e.g. the +cpu wheel).
DEVICE_ENV = "RAG_MODEL_DEVICE"
# A failed cross-encoder is retried after this window instead of being disabled for
# the process lifetime: a missing cache can be populated, and a transient CUDA error
# should not permanently downgrade answer quality.
RERANKER_RETRY_SECONDS = 60.0


def resolve_device() -> str:
    """Pick the torch device for the local models, preferring CUDA when present."""

    override = os.environ.get(DEVICE_ENV, "").strip().lower()
    if override:
        return override
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def _load_on_best_device(load):
    """Load a model on the preferred device, retrying once on CPU.

    A GPU load can still fail at runtime (VRAM pressure, driver hiccup); retrying on
    CPU keeps the semantic models available. If CPU also fails, the exception
    propagates — for the reranker that reaches the existing lexical fallback.
    """

    device = resolve_device()
    try:
        model = load(device)
    except Exception as error:
        if device == "cpu":
            raise
        logger.warning("模型无法在 %s 上加载，回退到 CPU: %s", device, error)
        device = "cpu"
        model = load(device)
    logger.info("模型运行设备: %s", device)
    return model, device


class BgeEmbedder:
    def __init__(self, model: object | None = None) -> None:
        self._model = model

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model, _ = _load_on_best_device(
                lambda device: SentenceTransformer(EMBEDDER_MODEL, local_files_only=True,
                                                   device=device)
            )
        vectors = self._model.encode(texts, normalize_embeddings=True)
        result = [[float(value) for value in row] for row in vectors]
        if len(result) != len(texts) or any(len(row) != 512 or not all(math.isfinite(x) for x in row) for row in result):
            raise ValueError("向量输出必须与输入对应，且为 512 维有限数值")
        return result


class BgeReranker:
    def __init__(self, model: object | None = None) -> None:
        self._model = model
        self._unavailable_until = 0.0

    @property
    def available(self) -> bool:
        """Whether the cross-encoder is currently usable (not in a failure cooldown)."""
        return not self._in_cooldown()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def status(self) -> dict[str, object]:
        """Sanitized availability state for ``/health``; never exposes raw errors."""
        cooldown = self._in_cooldown()
        return {
            "available": not cooldown,
            "loaded": self.loaded,
            "reason": "reranker_unavailable" if cooldown else None,
            "retry_in_seconds": (round(max(0.0, self._unavailable_until - monotonic()), 1)
                                 if cooldown else 0.0),
        }

    def _in_cooldown(self) -> bool:
        return monotonic() < self._unavailable_until

    def score(self, question: str, texts: list[str]) -> list[float]:
        return self.score_with_kind(question, texts)[0]

    def score_with_kind(self, question: str, texts: list[str]) -> tuple[list[float], str]:
        """Score passages and report which producer the scores came from.

        The returned kind is ``rerank`` only when the cross-encoder actually ran.
        Any failure degrades to ``lexical`` scores, which downstream gating must
        judge with its own conservative policy rather than the reranker threshold.
        """
        if not texts:
            return [], SCORE_RERANK
        if self._in_cooldown():
            return self._fallback_scores(question, texts), SCORE_LEXICAL

        try:
            if self._model is None:
                from sentence_transformers import CrossEncoder

                self._model, _ = _load_on_best_device(
                    lambda device: CrossEncoder(RERANKER_MODEL, local_files_only=True,
                                                max_length=RERANKER_MAX_LENGTH,
                                                model_kwargs={"torch_dtype": RERANKER_DTYPE},
                                                device=device)
                )
            logits = self._model.predict([[question, text] for text in texts])
            scores = [1.0 / (1.0 + math.exp(-float(value))) for value in logits]
            if len(scores) != len(texts) or not all(math.isfinite(score) for score in scores):
                raise ValueError("重排分数无效")
            self._unavailable_until = 0.0
            return scores, SCORE_RERANK
        except Exception as error:
            self._unavailable_until = monotonic() + RERANKER_RETRY_SECONDS
            logger.warning("重排模型不可用，降级为词面覆盖率评分，%.0f 秒后重试: %s",
                           RERANKER_RETRY_SECONDS, error)
            return self._fallback_scores(question, texts), SCORE_LEXICAL

    @staticmethod
    def _fallback_scores(question: str, texts: list[str]) -> list[float]:
        """Score candidates by conservative query content-word coverage.

        The cross-encoder's 0.5 gate threshold is intentionally not reused: lexical
        coverage is a different scale. A chunk sharing a single character with the
        query scores 0, and everything else is capped by how much of the query it
        actually covers, so unrelated chunks cannot pass evidence gating when the
        reranker is down.
        """

        return [lexical_score(question, text) for text in texts]
