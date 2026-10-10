"""Evidence threshold gate."""

from ...generation import ABSTENTION
from ..contracts import CAPABILITY_WEB_SEARCH, ExecutionContext, supports


def gate(inputs: dict, config: dict, context: ExecutionContext, threshold: float) -> dict:
    cutoff = float(config.get("threshold", threshold))
    if not 0 <= cutoff <= 1:
        raise ValueError("证据阈值必须在 0 到 1 之间")
    evidence = [item for item in inputs["evidence"] if item.score >= cutoff]
    # 联网是全局内置能力，不是本节点的开关：运行时能触网时，检索落空交给生成阶段兜底而不是过早拒答。
    fallback = supports(context.capabilities, CAPABILITY_WEB_SEARCH) and not evidence
    result = {"evidence": evidence, "abstained": bool(not evidence and not fallback)}
    if result["abstained"]:
        result.update({"answer": ABSTENTION, "citations": []})
    return result
