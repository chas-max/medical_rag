"""Reranking subquery aggregation, quota diversification, and multi-retrieval priors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .pdf_sections import is_administrative_boilerplate
from .query_normalization import extract_guideline_titles, filename_matches_title
from .types import SCORE_RERANK, Evidence

_QA_GUIDE_MARKER = "问答"


def _parent_identifier(item: Evidence) -> str:
    return item.chunk.parent_id or item.chunk.id


def _rrf_signal(item: Evidence) -> float:
    if item.fused_score:
        return float(item.fused_score)
    return float(item.score) if item.score_kind != SCORE_RERANK else 0.0


def apply_multi_priors(
    fused: list[Evidence],
    queries: list[str],
    config: dict,
) -> list[Evidence]:
    """Adjust fused candidate priors before rerank truncation without mutating scores when unconfigured."""
    filename_boost = float(config.get("filename_boost_weight", 0.0))
    qa_dampen = float(config.get("qa_dampen_factor", 1.0))
    boilerplate_penalty = float(config.get("boilerplate_penalty", 1.0))
    if filename_boost == 0.0 and qa_dampen == 1.0 and boilerplate_penalty == 1.0:
        return fused

    combined_query = " ".join(q for q in queries if isinstance(q, str))
    titles = extract_guideline_titles(combined_query) if filename_boost > 0.0 else []
    asks_qa = _QA_GUIDE_MARKER in combined_query

    adjusted: list[tuple[float, Evidence]] = []
    for item in fused:
        base = float(item.fused_score or item.score)
        multiplier = 1.0
        chunk = item.chunk
        if titles and filename_matches_title(chunk.filename, titles):
            if _QA_GUIDE_MARKER in chunk.filename and not asks_qa and not any(_QA_GUIDE_MARKER in t for t in titles):
                multiplier *= 1.0 + filename_boost * 0.3
            else:
                multiplier *= 1.0 + filename_boost
        if qa_dampen != 1.0 and _QA_GUIDE_MARKER in chunk.filename and not asks_qa:
            multiplier *= qa_dampen
        if boilerplate_penalty != 1.0 and is_administrative_boilerplate(chunk.text, chunk.section):
            multiplier *= boilerplate_penalty
        new_score = base * multiplier
        adjusted.append((new_score, replace(item, fused_score=new_score)))

    adjusted.sort(key=lambda pair: pair[0], reverse=True)
    return [pair[1] for pair in adjusted]


def _boilerplate_multiplier(item: Evidence, boilerplate_factor: float) -> float:
    if boilerplate_factor != 1.0 and is_administrative_boilerplate(item.chunk.text, item.chunk.section):
        return boilerplate_factor
    return 1.0


def select_with_quotas(
    pool: list[Evidence],
    scores: list[float],
    score_kind: str,
    min_score: float,
    inputs: dict,
    config: dict,
    reranker: object,
    docs: list[str],
    limit: int,
    select_diverse_fn: Callable[[list[Evidence], int, int, bool], list[Evidence]],
) -> list[Evidence]:
    """Score subqueries when configured and preserve calibrated Cross-Encoder scores.

    Evidence.score 始终保持纯净的交叉编码器校准分数 s_agg，以便 downstream evidence.gate
    进行可信的拐点剪枝；RRF 先验仅作用于排序位次而不污染评分尺度。
    """
    max_per_parent = int(config.get("max_per_parent", 0))
    backfill = bool(config.get("backfill_same_parent", True))
    sub_weight = float(config.get("subquery_blend_weight", 0.0))
    rrf_weight = float(config.get("rrf_blend_weight", 0.0))
    sub_quota = int(config.get("subquery_quota", 0))
    boilerplate_factor = float(config.get("boilerplate_factor", 1.0))

    raw_subs = inputs.get("subqueries")
    main_q = str(inputs.get("question", "")).strip()
    subqueries = [
        item.strip()
        for item in (raw_subs if isinstance(raw_subs, list) else [])
        if isinstance(item, str) and item.strip() and item.strip() != main_q
    ][:3]

    if score_kind != SCORE_RERANK or (sub_weight == 0.0 and rrf_weight == 0.0 and sub_quota == 0):
        ranked: list[Evidence] = []
        for item, score in zip(pool, scores):
            adj_score = float(score) * _boilerplate_multiplier(item, boilerplate_factor)
            if adj_score >= min_score:
                ranked.append(replace(item, score=adj_score, score_kind=score_kind))
        ordered = sorted(ranked, key=lambda item: item.score, reverse=True)
        return select_diverse_fn(ordered, limit, max_per_parent, backfill)

    sub_rows: list[list[float]] = []
    if subqueries and (sub_weight > 0.0 or sub_quota > 0):
        for sq in subqueries:
            if hasattr(reranker, "score_with_kind"):
                sq_scores, _ = reranker.score_with_kind(sq, docs)
            else:
                sq_scores = reranker.score(sq, docs)
            sub_rows.append([float(val) for val in sq_scores])

    raw_rrf = [_rrf_signal(item) for item in pool]
    max_rrf = max(raw_rrf) if raw_rrf and max(raw_rrf) > 0.0 else 1.0
    bp_factors = [_boilerplate_multiplier(item, boilerplate_factor) for item in pool]

    # (rank_score, ev_with_s_agg, original_idx)
    candidates: list[tuple[float, Evidence, int]] = []
    for idx, (item, raw_main) in enumerate(zip(pool, scores)):
        bp = bp_factors[idx]
        s_main = float(raw_main) * bp
        if sub_rows and sub_weight > 0.0:
            s_sub_max = max(row[idx] for row in sub_rows) * bp
            blended = (1.0 - sub_weight) * s_main + sub_weight * s_sub_max
            s_agg = max(s_main, blended)
        else:
            s_agg = s_main
        if s_agg < min_score:
            continue
        norm_rrf = raw_rrf[idx] / max_rrf
        sort_score = s_agg + rrf_weight * norm_rrf
        ev = replace(item, score=s_agg, score_kind=score_kind)
        candidates.append((sort_score, ev, idx))

    if not candidates or limit < 1:
        return []
    candidates.sort(key=lambda entry: entry[0], reverse=True)

    selected: list[Evidence] = []
    seen_ids: set[str] = set()
    parent_counts: dict[str, int] = {}

    def try_add(ev: Evidence) -> bool:
        if len(selected) >= limit or ev.chunk.id in seen_ids:
            return False
        parent_id = _parent_identifier(ev)
        if max_per_parent > 0 and parent_counts.get(parent_id, 0) >= max_per_parent:
            return False
        seen_ids.add(ev.chunk.id)
        parent_counts[parent_id] = parent_counts.get(parent_id, 0) + 1
        selected.append(ev)
        return True

    if sub_quota > 0 and sub_rows:
        for row in sub_rows:
            per_sub = sorted(candidates, key=lambda entry: row[entry[2]] * bp_factors[entry[2]], reverse=True)
            taken = 0
            for _, ev, idx in per_sub:
                if row[idx] * bp_factors[idx] < min_score:
                    continue
                if try_add(ev):
                    taken += 1
                    if taken >= sub_quota or len(selected) >= limit:
                        break

    for _, ev, _ in candidates:
        if len(selected) >= limit:
            break
        try_add(ev)

    if backfill and len(selected) < limit:
        for _, ev, _ in candidates:
            if len(selected) >= limit:
                break
            if ev.chunk.id not in seen_ids:
                seen_ids.add(ev.chunk.id)
                selected.append(ev)

    selected.sort(key=lambda item: item.score, reverse=True)
    return selected[:limit]
