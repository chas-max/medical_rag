"""Deterministic retrieval metrics over ranked, labeled chunk IDs and parent windows."""

from math import isfinite, log2
from typing import Any

from .evaluation_anchors import _comparable


def _graded_ndcg(
    ranked: list[str],
    relevant_ids: set[str],
    grades: dict[str, int],
    k: int,
) -> float:
    """Compute NDCG@k using exponential gain 2^grade - 1."""
    dcg = sum(
        ((2 ** grades.get(block_id, 2)) - 1) / log2(pos + 1)
        for pos, block_id in enumerate(ranked, 1)
        if block_id in relevant_ids
    )
    ideal_gains = sorted(
        ((2 ** grades.get(cid, 2)) - 1 for cid in relevant_ids),
        reverse=True,
    )[:k]
    idcg = sum(gain / log2(pos + 1) for pos, gain in enumerate(ideal_gains, 1))
    return dcg / idcg if idcg > 0 else 0.0


def retrieval_scores(
    ranked_ids: list[str],
    relevant_ids: set[str],
    k: int,
    *,
    chunk_grades: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Score the first k unique retrieved IDs against binary or graded labels."""
    if k < 1 or not relevant_ids:
        raise ValueError("k and relevant IDs must be nonempty")
    ranked = list(dict.fromkeys(ranked_ids))[:k]
    grades = {cid: int(chunk_grades.get(cid, 2)) for cid in relevant_ids} if chunk_grades else {}
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
    core_ids = {cid for cid in relevant_ids if grades.get(cid, 2) >= 2} or relevant_ids
    core_hits = sum(1 for block_id in hits if block_id in core_ids)
    core_recall = core_hits / len(core_ids)
    ndcg = _graded_ndcg(ranked, relevant_ids, grades, k)
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "ap": precision_sum / min(len(relevant_ids), k),
        "rr": reciprocal_rank,
        "ndcg": ndcg,
        "core_recall": core_recall,
        "hit_rate": 1.0 if hits else 0.0,
        "retrieved_count": len(ranked),
        "hit_ids": hits,
    }


def mean_or_none(values: list[float]) -> float | None:
    """Average valid scores; signal an empty sample explicitly."""
    if any(not isfinite(value) for value in values):
        raise ValueError("scores must be finite")
    return sum(values) / len(values) if values else None


def _parent_window_key(item: Any) -> str:
    chunk = getattr(item, "chunk", item)
    parent_id = getattr(chunk, "parent_id", "") or ""
    if parent_id:
        return parent_id
    return str(getattr(chunk, "id", getattr(chunk, "chunk_id", id(item))))


def _parent_context_text(item: Any) -> str:
    chunk = getattr(item, "chunk", item)
    return str(getattr(chunk, "context_text", "") or getattr(chunk, "parent_text", "") or getattr(chunk, "text", ""))


def _chunk_id(item: Any) -> str:
    chunk = getattr(item, "chunk", item)
    return str(getattr(chunk, "id", getattr(chunk, "chunk_id", "")))


def parent_window_scores(
    evidence: list[Any],
    relevant_ids: set[str],
    k: int,
    *,
    chunk_grades: dict[str, int] | None = None,
    source_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score deduplicated parent windows with R-Precision and quote/ID recall."""
    if k < 1 or (not relevant_ids and not source_refs):
        raise ValueError("k and relevant targets must be nonempty")

    windows: list[Any] = []
    seen_parents: set[str] = set()
    for item in evidence:
        key = _parent_window_key(item)
        if key in seen_parents:
            continue
        seen_parents.add(key)
        windows.append(item)
        if len(windows) >= k:
            break

    normalized_refs: list[tuple[int, str, int]] = []
    if source_refs:
        for idx, ref in enumerate(source_refs):
            quote = _comparable(str(ref.get("quote", "")))
            if quote:
                normalized_refs.append((idx, quote, int(ref.get("relevance", 2))))

    grades = {cid: int(chunk_grades.get(cid, 2)) for cid in relevant_ids} if chunk_grades else {}
    hit_positions: list[int] = []
    hit_ids: list[str] = []
    covered_refs: set[int] = set()
    covered_ids: set[str] = set()
    dcg = 0.0

    for pos, item in enumerate(windows, 1):
        cid = _chunk_id(item)
        if normalized_refs:
            ctx = _comparable(_parent_context_text(item))
            matched = [idx for idx, quote, _ in normalized_refs if quote in ctx]
            new_matches = [idx for idx in matched if idx not in covered_refs]
            is_hit = bool(matched) or (cid in relevant_ids)
            if is_hit:
                hit_positions.append(pos)
                hit_ids.append(cid)
                covered_refs.update(matched)
                if cid in relevant_ids:
                    covered_ids.add(cid)
                if new_matches:
                    gain_grade = max(normalized_refs[idx][2] for idx in new_matches)
                elif cid in relevant_ids and not matched:
                    gain_grade = grades.get(cid, 2)
                else:
                    gain_grade = 0
                if gain_grade > 0:
                    dcg += ((2 ** gain_grade) - 1) / log2(pos + 1)
        else:
            is_hit = cid in relevant_ids
            if is_hit:
                hit_positions.append(pos)
                hit_ids.append(cid)
                is_new = cid not in covered_ids
                covered_ids.add(cid)
                if is_new:
                    gain_grade = grades.get(cid, 2)
                    dcg += ((2 ** gain_grade) - 1) / log2(pos + 1)

    precision = len(hit_positions) / len(windows) if windows else 0.0
    if normalized_refs:
        r_total = len(normalized_refs)
        ideal_grades = sorted((grade for _, _, grade in normalized_refs), reverse=True)[:k]
        # 若窗口不含引用文本而通过 chunk_id 命中（如轻量单测夹具），仅当未匹配到引文时兜底计入覆盖
        if covered_refs:
            recall = len(covered_refs) / len(normalized_refs)
            core_indices = {idx for idx, _, grade in normalized_refs if grade >= 2} or {
                idx for idx, _, _ in normalized_refs
            }
            core_recall = len(covered_refs & core_indices) / len(core_indices)
        elif hit_ids and relevant_ids:
            recall = len(covered_ids) / len(relevant_ids)
            core_ids = {cid for cid in relevant_ids if grades.get(cid, 2) >= 2} or relevant_ids
            core_recall = len(covered_ids & core_ids) / len(core_ids)
        else:
            recall = 0.0
            core_recall = 0.0
    else:
        r_total = len(relevant_ids)
        recall = len(covered_ids) / len(relevant_ids)
        core_ids = {cid for cid in relevant_ids if grades.get(cid, 2) >= 2} or relevant_ids
        core_recall = len(covered_ids & core_ids) / len(core_ids)
        ideal_grades = sorted((grades.get(cid, 2) for cid in relevant_ids), reverse=True)[:k]

    idcg = sum(((2 ** grade) - 1) / log2(pos + 1) for pos, grade in enumerate(ideal_grades, 1))
    ndcg = dcg / idcg if idcg > 0 else 0.0
    r_hits = sum(1 for pos in hit_positions if pos <= r_total)
    r_precision = min(1.0, r_hits / r_total) if r_total > 0 and windows else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": precision,
        "r_precision": r_precision,
        "recall": recall,
        "core_recall": core_recall,
        "f1": f1,
        "rr": 1 / hit_positions[0] if hit_positions else 0.0,
        "ndcg": ndcg,
        "hit_rate": 1.0 if hit_positions else 0.0,
        "retrieved_count": len(windows),
        "hit_ids": hit_ids,
    }


def cited_retrieval_scores(
    cited_ids: list[str],
    evidence: list[Any],
    relevant_ids: set[str],
    *,
    chunk_grades: dict[str, int] | None = None,
    source_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate Post-RAG precision/recall strictly on the blocks actually cited in the answer."""
    ordered_cited = list(dict.fromkeys(cid for cid in cited_ids if cid))
    if not ordered_cited:
        return {
            "precision": 0.0,
            "recall": 0.0,
            "core_recall": 0.0,
            "f1": 0.0,
            "hit_rate": 0.0,
            "child_precision": 0.0,
            "child_recall": 0.0,
            "child_f1": 0.0,
            "cited_count": 0,
        }
    ev_by_id = {_chunk_id(item): item for item in evidence}
    cited_evidence = [ev_by_id[cid] for cid in ordered_cited if cid in ev_by_id]
    child = retrieval_scores(
        ordered_cited,
        relevant_ids,
        max(1, len(ordered_cited)),
        chunk_grades=chunk_grades,
    )
    if not cited_evidence:
        return {
            "precision": child["precision"],
            "recall": child["recall"],
            "core_recall": child["core_recall"],
            "f1": child["f1"],
            "hit_rate": child["hit_rate"],
            "child_precision": child["precision"],
            "child_recall": child["recall"],
            "child_f1": child["f1"],
            "cited_count": len(ordered_cited),
        }
    parent = parent_window_scores(
        cited_evidence,
        relevant_ids,
        max(1, len(cited_evidence)),
        chunk_grades=chunk_grades,
        source_refs=source_refs,
    )
    return {
        "precision": parent["precision"],
        "recall": parent["recall"],
        "core_recall": parent["core_recall"],
        "f1": parent["f1"],
        "hit_rate": parent["hit_rate"],
        "child_precision": child["precision"],
        "child_recall": child["recall"],
        "child_f1": child["f1"],
        "cited_count": len(ordered_cited),
    }
