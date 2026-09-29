"""Separate untrusted model drafting from citation validation."""

from ..contracts import ExecutionContext


def draft(inputs: dict, config: dict, context: ExecutionContext, generator: object) -> dict:
    result = generator.generate(inputs["question"], inputs["evidence"], inputs["provider"],
                                web_search=bool(config.get("web_search", False)))
    # Web hits are evidence only for this node, so citation checking must see the same set.
    return {"draft": result["draft"], "evidence": result["evidence"]}


def validate(inputs: dict, config: dict, context: ExecutionContext, generator: object) -> dict:
    return {"answer": generator.validate(inputs["draft"], inputs["evidence"])}
