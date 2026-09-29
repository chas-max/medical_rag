"""Evidence threshold gate."""

from ...generation import ABSTENTION
from ..contracts import ExecutionContext


def gate(inputs: dict, config: dict, context: ExecutionContext, threshold: float,
         web_search_available: bool = False) -> dict:
    cutoff = float(config.get("threshold", threshold))
    if not 0 <= cutoff <= 1:
        raise ValueError("证据阈值必须在 0 到 1 之间")
    evidence = [item for item in inputs["evidence"] if item.score >= cutoff]
    # With a searchable generation step, empty retrieval is handed over instead of refused here.
    fallback = bool(config.get("allow_web_search")) and web_search_available and not evidence
    result = {"evidence": evidence, "abstained": bool(not evidence and not fallback)}
    if result["abstained"]:
        result.update({"answer": ABSTENTION, "citations": []})
    return result
