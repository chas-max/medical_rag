"""Function-calling tools the generation agent may invoke."""

from collections.abc import Callable
from dataclasses import dataclass
import json

from .config import Settings
from .types import ApiError, Evidence
from .web_search import TavilySearch

TAVILY_TOOL = "tavily_search"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolOutcome:
    """What the model reads back, plus evidence the caller may still verify."""

    payload: dict
    evidence: tuple[Evidence, ...] = ()
    query: str | None = None
    failed: bool = False

    @property
    def content(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], ToolOutcome]

    def spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self, tools: list[Tool] | tuple[Tool, ...] = ()) -> None:
        registered: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in registered:
                raise ValueError(f"tool already registered: {tool.name}")
            registered[tool.name] = tool
        self._tools = registered

    def __bool__(self) -> bool:
        return bool(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict]:
        return [self._tools[name].spec() for name in sorted(self._tools)]

    def execute(self, name: str, arguments: str | dict | None) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutcome({"error": "UNKNOWN_TOOL", "message": "该工具当前不可用"}, failed=True)
        if isinstance(arguments, dict):
            payload = arguments
        elif isinstance(arguments, str) and arguments.strip():
            try:
                payload = json.loads(arguments)
            except ValueError:
                return ToolOutcome({"error": "INVALID_TOOL_ARGUMENTS", "message": "工具参数不是有效 JSON"},
                                   failed=True)
        else:
            payload = {}
        if not isinstance(payload, dict):
            return ToolOutcome({"error": "INVALID_TOOL_ARGUMENTS", "message": "工具参数必须是 JSON 对象"},
                               failed=True)
        return tool.handler(payload)


def tavily_search_tool(search: object) -> Tool:
    def run(arguments: dict) -> ToolOutcome:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolOutcome({"error": "INVALID_TOOL_ARGUMENTS", "message": "缺少检索问题"}, failed=True)
        clean = query.strip()
        try:
            pages = search.search(clean)
        except ApiError as error:
            return ToolOutcome({"error": error.code, "message": error.message}, failed=True)
        evidence = search.to_evidence(pages)
        results = [{"chunk_id": item.chunk.id, "origin": item.origin, "title": item.chunk.filename,
                    "url": item.chunk.source_id, "content": item.chunk.text, "score": item.score}
                   for item in evidence]
        return ToolOutcome({"query": clean, "results": results}, tuple(evidence), clean)

    return Tool(
        TAVILY_TOOL,
        "当本地资料缺失或不足以支撑结论时，检索公开网页资料；返回带 chunk_id 的原文摘录，可作为引用来源。",
        {"type": "object",
         "properties": {"query": {"type": "string", "description": "需要联网检索的具体问题"}},
         "required": ["query"], "additionalProperties": False},
        run,
    )


def build_tools(settings: Settings, search: object | None = None) -> ToolRegistry | None:
    """Expose tools only when the external search is configured."""
    if not settings.tavily_api_key:
        return None
    return ToolRegistry([tavily_search_tool(search or TavilySearch(settings))])
