"""Evidence threshold gate."""

from ...generation import (
    ABSTAIN_DEGRADED,
    ABSTAIN_LOW_SCORE,
    ABSTAIN_NO_EVIDENCE,
    ABSTENTION,
    DEGRADED_ABSTENTION,
)
from ...lexical import LEXICAL_MIN_COVERAGE
from ...types import SCORE_DENSE, SCORE_LEXICAL, SCORE_RERANK, SCORE_UNKNOWN
from ..contracts import (
    CAPABILITY_WEB_SEARCH,
    RERANK_DEGRADED_VARIABLE,
    ExecutionContext,
    Halt,
    supports,
)


def gate(inputs: dict, config: dict, context: ExecutionContext,
         threshold: float, fail_closed: bool = True) -> dict | Halt:
    cutoff = float(config.get("threshold", threshold))
    if not 0 <= cutoff <= 1:
        raise ValueError("证据阈值必须在 0 到 1 之间")
    degraded = context.variables.get(RERANK_DEGRADED_VARIABLE)
    evidence = []
    for item in inputs["evidence"]:
        kind = getattr(item, "score_kind", SCORE_UNKNOWN)
        if kind in (SCORE_RERANK, SCORE_DENSE):
            # 重排分数按校准阈值；dense 是“直连门控”的独立策略，保留它自己的
            # 余弦阈值，不与词面覆盖率混用。
            if item.score >= cutoff:
                evidence.append(item)
        elif kind == SCORE_LEXICAL:
            degraded = degraded or {"reason": "reranker_unavailable", "score_kind": kind}
            # 默认 fail-closed：重排不可用时不拿词面分数生成答案。显式关闭后也只
            # 接受“查询实词覆盖率高”的块，且该分数已在重排降级时按同一下限计算。
            if not fail_closed and item.score >= LEXICAL_MIN_COVERAGE:
                evidence.append(item)
        # bm25 / web / unknown 的分数量纲与阈值不可比，一律不进入答案门控。
    # 联网是全局内置能力，不是本节点的开关：运行时能触网时，检索落空交给生成阶段兜底而不是过早拒答。
    fallback = supports(context.capabilities, CAPABILITY_WEB_SEARCH) and not evidence
    if evidence or fallback:
        result = {"evidence": evidence, "abstained": False}
        if degraded:
            result["degraded"] = True
            result["degraded_reason"] = degraded.get("reason", "reranker_unavailable")
        return result
    # 拒答原因写进输出与 trace：只有“检索为空/阈值没过”才算资料不足，
    # 检索降级要说成降级，二者不能混成同一句话。
    if degraded:
        reason = degraded.get("reason", "reranker_unavailable")
        context.node_metadata.update(abstain_reason=ABSTAIN_DEGRADED, degraded=True, reason=reason)
        return Halt({"evidence": [], "abstained": True, "answer": DEGRADED_ABSTENTION,
                     "citations": [], "abstain_reason": ABSTAIN_DEGRADED,
                     "degraded": True, "degraded_reason": reason})
    # 有候选但一条都没过阈值 = 低分；输入本来就为空 = 无证据。两者都不是生成故障。
    reason = ABSTAIN_LOW_SCORE if inputs["evidence"] else ABSTAIN_NO_EVIDENCE
    context.node_metadata.update(abstain_reason=reason)
    # 拒答是控制流而不是数据：halt 让引擎剪掉本节点的下游，并把这份答案作为运行结果。
    return Halt({"evidence": evidence, "abstained": True, "answer": ABSTENTION,
                 "citations": [], "abstain_reason": reason})
