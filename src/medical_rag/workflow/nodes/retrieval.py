"""Dense/BM25 retrieval, reciprocal rank fusion, and reranking."""

from dataclasses import replace
from fractions import Fraction

from ...types import Evidence
from ..contracts import ExecutionContext


def dense(inputs: dict, config: dict, context: ExecutionContext, store: object, embedder: object,
          candidate_k: int) -> dict:
    vector = embedder.encode([inputs["question"]])[0]
    return {"evidence": store.search_dense(vector, int(config.get("top_k", candidate_k)))}


def bm25(inputs: dict, config: dict, context: ExecutionContext, store: object,
         candidate_k: int) -> dict:
    return {"evidence": store.search_bm25(inputs["question"], int(config.get("top_k", candidate_k)))}


def rrf(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    """Fuse any number of retrieval lists; the graph decides how many there are."""
    k = int(config.get("k", 60))
    if k < 1:
        raise ValueError("RRF k 必须大于零")
    fused: dict[str, Evidence] = {}
    for candidates in inputs["sources"]:
        for rank, item in enumerate(candidates, 1):
            old = fused.get(item.chunk.id)
            fused[item.chunk.id] = replace(item, fused_score=(old.fused_score if old else 0.0) + 1 / (k + rank))
    return {"evidence": sorted(fused.values(), key=lambda item: item.fused_score, reverse=True)}


def rerank(inputs: dict, config: dict, context: ExecutionContext, reranker: object,
           threshold: float, rerank_k: int, top_k: int) -> dict:
    evidence = inputs["evidence"]
    if not evidence:
        return {"evidence": []}
    # 节点配置优先，其次是从入参连过来的运行级 top_k，最后才是服务默认值。
    limit = int(config.get("top_k", inputs.get("top_k", top_k)))
    # Cross-encoding every fused candidate costs tens of seconds on CPU, so score the
    # RRF head only; a top_k beyond the cap widens the pool instead of truncating results.
    pool = evidence[:max(rerank_k, limit)]
    scores = reranker.score(inputs["question"], [item.chunk.text for item in pool])
    if len(scores) != len(pool):
        raise ValueError("重排分数数量与候选不一致")
    cutoff = float(config.get("threshold", threshold))
    ranked = [replace(item, score=float(score)) for item, score in zip(pool, scores)
              if float(score) >= cutoff]
    return {"evidence": sorted(ranked, key=lambda item: item.score, reverse=True)[:limit]}


def multi(inputs: dict, config: dict, context: ExecutionContext, store: object,
          embedder: object, candidate_k: int) -> dict:
    """Search every connected query with the chosen strategy, then fuse the lists.

    `queries` 是 collect 端口，所以连线数就是检索路数：一个问题走 dense，
    分解出的三个子问题各走 dense+bm25，都只是图上的连线差别。
    """
    strategy = str(config.get("strategy", "hybrid"))
    k = int(config.get("k", 60))
    if k < 1:
        raise ValueError("RRF k 必须大于零")
    ranked_lists = []
    for question in inputs["queries"]:
        if strategy in ("hybrid", "dense"):
            ranked_lists.append(store.search_dense(embedder.encode([question])[0], candidate_k))
        if strategy in ("hybrid", "bm25"):
            ranked_lists.append(store.search_bm25(question, candidate_k))
    if not ranked_lists:
        raise ValueError("没有可用的检索策略")
    fused: dict[str, Evidence] = {}
    scores: dict[str, Fraction] = {}
    for candidates in ranked_lists:
        for rank, item in enumerate(candidates, 1):
            key = item.chunk.id
            scores[key] = scores.get(key, Fraction(0)) + Fraction(1, k + rank)
            fused[key] = item
    evidence = [replace(item, fused_score=float(scores[key])) for key, item in fused.items()]
    return {"evidence": sorted(evidence, key=lambda item: item.fused_score,
                               reverse=True)}
