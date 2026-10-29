"""Public result shaping nodes."""

from ..contracts import ExecutionContext


def answer(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    return inputs["answer"]


def search(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    return {"evidence": inputs["evidence"]}


def document(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    return inputs["result"]
