"""Evidence threshold gate."""

from ...generation import ABSTENTION
from ..contracts import ExecutionContext


def gate(inputs: dict, config: dict, context: ExecutionContext, threshold: float) -> dict:
    cutoff = float(config.get("threshold", threshold))
    if not 0 <= cutoff <= 1:
        raise ValueError("证据阈值必须在 0 到 1 之间")
    evidence = [item for item in inputs["evidence"] if item.score >= cutoff]
    result = {"evidence": evidence, "abstained": not bool(evidence)}
    if not evidence:
        result.update({"answer": ABSTENTION, "citations": []})
    return result
