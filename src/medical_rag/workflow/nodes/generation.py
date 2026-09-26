"""Separate untrusted model drafting from citation validation."""

from ..contracts import ExecutionContext


def draft(inputs: dict, config: dict, context: ExecutionContext, generator: object) -> dict:
    return {"draft": generator.draft(inputs["question"], inputs["evidence"], inputs["provider"])}


def validate(inputs: dict, config: dict, context: ExecutionContext, generator: object) -> dict:
    return {"answer": generator.validate(inputs["draft"], inputs["evidence"])}
