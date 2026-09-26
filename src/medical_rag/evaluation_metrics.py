"""Deterministic retrieval metrics over ranked, labeled chunk IDs."""

from math import isfinite


def retrieval_scores(ranked_ids: list[str], relevant_ids: set[str], k: int) -> dict:
    """Score the first k unique retrieved IDs against nonempty binary labels."""
    if k < 1 or not relevant_ids:
        raise ValueError("k and relevant IDs must be nonempty")
    ranked = list(dict.fromkeys(ranked_ids))[:k]
    hits = []
    precision_sum = 0.0
    reciprocal_rank = 0.0
    for position, block_id in enumerate(ranked, 1):
        if block_id in relevant_ids:
            hits.append(block_id)
            precision_sum += len(hits) / position
            if not reciprocal_rank:
                reciprocal_rank = 1 / position
    precision = len(hits) / len(ranked) if ranked else 0.0
    recall = len(hits) / len(relevant_ids)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1,
            "ap": precision_sum / min(len(relevant_ids), k), "rr": reciprocal_rank,
            "retrieved_count": len(ranked), "hit_ids": hits}


def mean_or_none(values: list[float]) -> float | None:
    """Average valid scores; signal an empty sample explicitly."""
    if any(not isfinite(value) for value in values):
        raise ValueError("scores must be finite")
    return sum(values) / len(values) if values else None
