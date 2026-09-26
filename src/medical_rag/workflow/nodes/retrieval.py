"""Dense/BM25 retrieval, reciprocal rank fusion, and reranking."""

from dataclasses import replace

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
    k = int(config.get("k", 60))
    if k < 1:
        raise ValueError("RRF k 必须大于零")
    fused: dict[str, Evidence] = {}
    for candidates in (inputs["dense"], inputs["bm25"]):
        for rank, item in enumerate(candidates, 1):
            old = fused.get(item.chunk.id)
            fused[item.chunk.id] = replace(item, fused_score=(old.fused_score if old else 0.0) + 1 / (k + rank))
    return {"evidence": sorted(fused.values(), key=lambda item: item.fused_score, reverse=True)}


def rerank(inputs: dict, config: dict, context: ExecutionContext, reranker: object,
           threshold: float) -> dict:
    evidence = inputs["evidence"]
    if not evidence:
        return {"evidence": []}
    scores = reranker.score(inputs["question"], [item.chunk.text for item in evidence])
    if len(scores) != len(evidence):
        raise ValueError("重排分数数量与候选不一致")
    cutoff = float(config.get("threshold", threshold))
    ranked = [replace(item, score=float(score)) for item, score in zip(evidence, scores)
              if float(score) >= cutoff]
    return {"evidence": sorted(ranked, key=lambda item: item.score, reverse=True)[:inputs["top_k"]]}
