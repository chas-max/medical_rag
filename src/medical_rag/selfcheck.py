"""Self-check that judges retrieved evidence before drafting."""

from dataclasses import dataclass

from .types import SCORE_RERANK, Evidence

MODE_CLEAR = "clear"
MODE_VAGUE = "vague"
MODE_NONE = "none"


@dataclass(frozen=True)
class SelfCheckPolicy:
    clear_threshold: float = 0.75
    min_clear_evidence: int = 2


@dataclass(frozen=True)
class Decision:
    mode: str
    search: bool
    reason: str


def assess(evidence: list[Evidence], policy: SelfCheckPolicy, search_available: bool) -> Decision:
    """Only empty or weak retrieval may reach the network; confident retrieval answers directly."""
    # 只有交叉编码器分数能证明“证据清晰”。词面覆盖率、dense 距离和 BM25 分数量纲
    # 不同，不能拿来和 clear_threshold 比较，否则降级分数会抑制联网兜底。
    reranked = [item for item in evidence
                if getattr(item, "score_kind", "") == SCORE_RERANK]
    if not evidence:
        mode, reason = MODE_NONE, "no_evidence"
    elif (reranked
          and max(item.score for item in reranked) >= policy.clear_threshold
          and len(reranked) >= policy.min_clear_evidence):
        mode, reason = MODE_CLEAR, "evidence_confident"
    else:
        mode, reason = MODE_VAGUE, "evidence_weak"
    return Decision(mode, mode != MODE_CLEAR and bool(search_available), reason)
