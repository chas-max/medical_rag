"""Tavily-backed web search whose hits are turned into citable, marked evidence."""

import hashlib
from dataclasses import dataclass

import httpx

from .config import Settings
from .types import ORIGIN_WEB, ApiError, Chunk, Evidence

MAX_QUERY_CHARS = 400


@dataclass(frozen=True)
class WebPage:
    url: str
    title: str
    content: str
    score: float


def _chunk_id(url: str) -> str:
    return "web-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class TavilySearch:
    """Call Tavily without exposing upstream bodies, and label every hit as web origin."""

    def __init__(self, settings: Settings, client_factory: object | None = None) -> None:
        self.settings = settings
        self.client_factory = client_factory

    def search(self, query: str, max_results: int | None = None) -> list[WebPage]:
        clean = query.strip() if isinstance(query, str) else ""
        if not clean:
            raise ApiError("INVALID_QUERY", "联网检索问题不能为空", 422)
        if not self.settings.tavily_api_key:
            raise ApiError("WEB_SEARCH_NOT_CONFIGURED", "联网搜索 API Key 未配置", 503)
        factory = self.client_factory
        if factory is None:
            factory = httpx.Client
        payload = {
            "query": clean[:MAX_QUERY_CHARS],
            "search_depth": self.settings.tavily_search_depth,
            "max_results": max_results or self.settings.tavily_max_results,
            "include_answer": False,
            "include_raw_content": False,
        }
        headers = {"Authorization": f"Bearer {self.settings.tavily_api_key}",
                   "Content-Type": "application/json"}
        try:
            with factory(timeout=self.settings.tavily_timeout_seconds) as client:
                response = client.post(f"{self.settings.tavily_base_url}/search", json=payload,
                                       headers=headers)
                response.raise_for_status()
                body = response.json()
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            raise ApiError("WEB_SEARCH_UNAVAILABLE", "联网搜索服务不可用，请稍后重试", 503) from error
        except httpx.HTTPStatusError as error:
            raise self._status_error(error) from error
        except httpx.RequestError as error:
            raise ApiError("WEB_SEARCH_UNAVAILABLE", "联网搜索服务不可用，请稍后重试", 503) from error
        except ValueError as error:
            raise ApiError("WEB_SEARCH_UPSTREAM_ERROR", "联网搜索结果无法解析，请稍后重试", 502) from error
        return self._pages(body)

    def to_evidence(self, pages: list[WebPage]) -> list[Evidence]:
        evidence = []
        seen = set()
        for ordinal, page in enumerate(pages):
            text = page.content[: self.settings.web_content_max_chars].strip()
            block_id = _chunk_id(page.url)
            if not text or block_id in seen:
                continue
            seen.add(block_id)
            block = Chunk(block_id, ORIGIN_WEB, page.url, text, page.title, page.url, None, ordinal)
            evidence.append(Evidence(block, page.score, origin=ORIGIN_WEB))
        return evidence

    def _pages(self, body: object) -> list[WebPage]:
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise ApiError("WEB_SEARCH_UPSTREAM_ERROR", "联网搜索结果格式异常", 502)
        pages = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            content = item.get("content")
            if not isinstance(url, str) or not url.strip() or not isinstance(content, str) or not content.strip():
                continue
            title = item.get("title")
            if not isinstance(title, str) or not title.strip():
                title = httpx.URL(url).host
            score = item.get("score")
            pages.append(WebPage(url.strip(), title.strip(), content.strip(),
                                 float(score) if isinstance(score, (int, float)) else 0.0))
        return pages[: self.settings.tavily_max_results]

    @staticmethod
    def _status_error(error: httpx.HTTPStatusError) -> ApiError:
        status = error.response.status_code
        if status in (401, 403):
            return ApiError("WEB_SEARCH_AUTH_FAILED", "联网搜索认证或权限不足，请检查 Tavily 配置", 503)
        if status == 429:
            return ApiError("WEB_SEARCH_RATE_LIMITED", "联网搜索请求过于频繁，请稍后重试", 429)
        if 400 <= status < 500:
            return ApiError("WEB_SEARCH_INVALID_REQUEST", "联网搜索请求不被接受", 422)
        return ApiError("WEB_SEARCH_UPSTREAM_ERROR", "联网搜索服务暂时失败，请稍后重试", 502)
