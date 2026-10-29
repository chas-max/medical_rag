"""Two-leg hybrid retrieval with RRF fusion and local reranking."""

from dataclasses import replace

from .types import SCORE_LEXICAL, SCORE_RERANK, Evidence


class Retriever:
    def __init__(self, store: object, embedder: object, reranker: object,
                 candidate_k: int = 20, threshold: float = 0.5) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.candidate_k = candidate_k
        self.threshold = threshold

    def search(self, question: str, top_k: int) -> list[Evidence]:
        if not question.strip() or top_k < 1 or top_k > 20:
            raise ValueError("问题不能为空，top_k 必须在 1 到 20 之间")
        query_vector = self.embedder.encode([question])[0]
        dense = self.store.search_dense(query_vector, self.candidate_k)
        sparse = self.store.search_bm25(question, self.candidate_k)
        fused: dict[str, Evidence] = {}
        for candidates in (dense, sparse):
            for rank, candidate in enumerate(candidates, start=1):
                key = candidate.chunk.id
                prior = fused.get(key)
                fused[key] = replace(candidate, fused_score=(prior.fused_score if prior else 0.0) + 1 / (60 + rank))
        ordered = sorted(fused.values(), key=lambda item: item.fused_score, reverse=True)
        if not ordered:
            return []
        if hasattr(self.reranker, "score_with_kind"):
            scores, score_kind = self.reranker.score_with_kind(
                question, [item.chunk.text for item in ordered])
        else:
            scores, score_kind = self.reranker.score(
                question, [item.chunk.text for item in ordered]), SCORE_RERANK
        if len(scores) != len(ordered):
            raise ValueError("重排分数数量与候选不一致")
        # 这条遗留路径没有门控可做保守判定，降级时直接返回空结果（fail-closed）；
        # 生产链路由 workflow/nodes/evidence.py 按 score_kind 分流。
        if score_kind == SCORE_LEXICAL:
            return []
        ranked = [replace(item, score=score, score_kind=score_kind)
                  for item, score in zip(ordered, scores) if score >= self.threshold]
        return sorted(ranked, key=lambda item: item.score, reverse=True)[:top_k]
