"""Evidence threshold gate."""

from ...generation import ABSTENTION
from ..contracts import CAPABILITY_WEB_SEARCH, ExecutionContext, Halt, supports


def gate(inputs: dict, config: dict, context: ExecutionContext,
         threshold: float) -> dict | Halt:
    cutoff = float(config.get("threshold", threshold))
    if not 0 <= cutoff <= 1:
        raise ValueError("证据阈值必须在 0 到 1 之间")
    evidence = [item for item in inputs["evidence"] if item.score >= cutoff]
    # 联网是全局内置能力，不是本节点的开关：运行时能触网时，检索落空交给生成阶段兜底而不是过早拒答。
    fallback = supports(context.capabilities, CAPABILITY_WEB_SEARCH) and not evidence
    if evidence or fallback:
        return {"evidence": evidence, "abstained": False}
    # 拒答是控制流而不是数据：halt 让引擎剪掉本节点的下游，并把这份答案作为运行结果。
    return Halt({"evidence": evidence, "abstained": True, "answer": ABSTENTION,
                 "citations": []})
