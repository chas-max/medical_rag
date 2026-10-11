"""Graph-level fan-in helpers that replace the hardcoded branch selectors."""

from ..contracts import ExecutionContext


def collect(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    """Merge every connected producer into one ordered, de-duplicated list.

    连接几个生产者完全由图决定：没连线的分支不执行，也就不会贡献值。
    """
    values: list[str] = []
    for value in inputs.get("items") or []:
        text = str(value).strip()
        if text and text not in values:
            values.append(text)
    return {"values": values}
