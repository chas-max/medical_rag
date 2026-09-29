"""Self-check that judges retrieved evidence before drafting."""

from dataclasses import dataclass

from .types import Evidence

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
    if not evidence:
        mode, reason = MODE_NONE, "no_evidence"
    elif (max(item.score for item in evidence) >= policy.clear_threshold
          and len(evidence) >= policy.min_clear_evidence):
        mode, reason = MODE_CLEAR, "evidence_confident"
    else:
        mode, reason = MODE_VAGUE, "evidence_weak"
    return Decision(mode, mode != MODE_CLEAR and bool(search_available), reason)
