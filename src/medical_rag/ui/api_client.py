"""HTTP client for the local RAG backend used by the Streamlit chat page.

The legacy backend is not an authenticated multi-tenant service: it only accepts
loopback clients in ``unsafe-local-demo`` mode and additionally requires a
loopback ``Origin`` header on non-GET requests. This client enforces both before
any request leaves the process; tenant isolation lives in the Streamlit layer.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_QUESTION_CHARS = 20_000
CHAT_WORKFLOW_ID = "medical-answer"
STREAM_EVENT_NAMES = frozenset(
    {"chat.progress", "node.progress", "status", "claim", "analysis", "done", "error"}
)


class BackendError(RuntimeError):
    """A caller-facing backend failure with a stable code for the UI."""

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


def backend_origin(base_url: str) -> str:
    """Validate a loopback base URL and return the Origin header value to send."""
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("UI_API_BASE_URL 必须是完整的本机 http(s) 地址")
    if parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError("UI_API_BASE_URL 只允许本机回环地址（127.0.0.1 / localhost / ::1）")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("UI_API_BASE_URL 不能包含路径、查询串或片段")
    try:
        _ = parts.port
    except ValueError as error:
        raise ValueError("UI_API_BASE_URL 端口无效") from error
    return f"{parts.scheme}://{parts.netloc}"


def build_chat_payload(question: str, conversation_id: str, *, top_k: int = 5,
                       request_id: str | None = None) -> dict[str, Any]:
    """The exact body the React frontend sends to ``/chat/runs[/stream]``."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("问题不能为空")
    text = question.strip()
    if len(text) > MAX_QUESTION_CHARS:
        raise ValueError(f"问题不能超过 {MAX_QUESTION_CHARS} 字符")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("会话标识不能为空")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
        raise ValueError("top_k 必须在 1 到 20 之间")
    return {
        "request_id": request_id or uuid4().hex,
        "conversation_id": conversation_id,
        "workflow_id": CHAT_WORKFLOW_ID,
        "inputs": {"question": text, "provider": "qwen", "top_k": top_k},
        "memory_consent": False,
    }


def parse_sse_block(block: str) -> tuple[str, dict[str, Any]] | None:
    """Parse one Server-Sent Events block into ``(event, json-data)``."""
    event = "message"
    data_lines: list[str] = []
    for line in block.split("\n"):
        if not line or line.startswith(":"):
            continue
        field, _, raw = line.partition(":")
        if raw.startswith(" "):
            raw = raw[1:]
        if field == "event":
            event = raw
        elif field == "data":
            data_lines.append(raw)
    if not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except (TypeError, ValueError) as error:
        raise BackendError("STREAM_PARSE_ERROR", "RAG 服务返回了无法解析的流式响应") from error
    if not isinstance(payload, dict):
        raise BackendError("STREAM_PARSE_ERROR", "RAG 服务返回了无法解析的流式响应")
    return event, payload


def iter_sse_events(lines: Iterable[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield complete SSE events from an iterable of text lines."""
    buffer: list[str] = []
    for line in lines:
        if line == "":
            frame = parse_sse_block("\n".join(buffer))
            buffer = []
            if frame is not None:
                yield frame
            continue
        buffer.append(line)
    frame = parse_sse_block("\n".join(buffer))
    if frame is not None:
        yield frame


class RagApiClient:
    """Thin, explicit client for the endpoints the chat/stats pages need."""

    def __init__(self, base_url: str, *, timeout: float = 120.0, top_k: int = 5,
                 transport: httpx.BaseTransport | None = None) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("请求超时必须大于 0")
        self.origin = backend_origin(base_url)
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.top_k = top_k
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout, connect=5.0),
            trust_env=False,
            headers={"Origin": self.origin, "Accept": "application/json"},
            transport=transport,
        )

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RagApiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- low level ---------------------------------------------------------
    @staticmethod
    def _unwrap(response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError as error:
            raise BackendError("INVALID_RESPONSE", "RAG 服务返回了无法解析的响应",
                               response.status_code) from error
        if not isinstance(payload, dict) or payload.get("success") is not True:
            error_payload = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(error_payload, dict):
                error_payload = {}
            code = error_payload.get("code") if isinstance(error_payload.get("code"), str) else "REQUEST_FAILED"
            message = (error_payload.get("message")
                       if isinstance(error_payload.get("message"), str) and error_payload.get("message")
                       else f"请求失败（HTTP {response.status_code}）")
            raise BackendError(code, message, response.status_code)
        return payload.get("data")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as error:
            raise BackendError("BACKEND_TIMEOUT", "RAG 服务响应超时，请稍后重试", 0) from error
        except httpx.HTTPError as error:
            raise BackendError(
                "BACKEND_UNREACHABLE",
                "无法连接 RAG 服务：请确认已运行 start.ps1 或 api_server（本机 8001）",
                0,
            ) from error
        return self._unwrap(response)

    # -- read endpoints ----------------------------------------------------
    def health(self) -> dict[str, Any]:
        data = self._request("GET", "/health")
        return data if isinstance(data, dict) else {}

    def document_jobs(self, *, active: bool = False, limit: int = 20) -> list[dict[str, Any]]:
        data = self._request("GET", "/document-jobs",
                             params={"active": "true" if active else "false",
                                     "limit": max(1, min(int(limit), 100))})
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def conversations(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/conversations")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/conversations/{conversation_id}")
        return data if isinstance(data, dict) else {}

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        data = self._request("DELETE", f"/conversations/{conversation_id}")
        return data if isinstance(data, dict) else {}

    def chunk(self, chunk_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/chunks/{chunk_id}")
        return data if isinstance(data, dict) else {}

    # -- chat --------------------------------------------------------------
    def stream_chat(self, question: str, conversation_id: str, *,
                    top_k: int | None = None,
                    on_event: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Run one chat turn over SSE and return the authoritative done result."""
        payload = build_chat_payload(question, conversation_id,
                                     top_k=self.top_k if top_k is None else top_k)
        result: dict[str, Any] | None = None
        try:
            with self._client.stream(
                "POST", "/chat/runs/stream", json=payload,
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(self.timeout, connect=5.0),
            ) as response:
                if response.status_code != 200:
                    content = response.read()
                    try:
                        parsed = json.loads(content.decode("utf-8"))
                    except (UnicodeError, ValueError) as error:
                        raise BackendError(
                            "REQUEST_FAILED",
                            f"问答请求失败（HTTP {response.status_code}）",
                            response.status_code,
                        ) from error
                    if isinstance(parsed, dict) and parsed.get("success") is not True:
                        error_payload = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
                        raise BackendError(
                            str(error_payload.get("code") or "REQUEST_FAILED"),
                            str(error_payload.get("message") or f"问答请求失败（HTTP {response.status_code}）"),
                            response.status_code,
                        )
                    raise BackendError("REQUEST_FAILED", "问答请求失败", response.status_code)
                for event, data in iter_sse_events(response.iter_lines()):
                    if event == "error":
                        raise BackendError(
                            str(data.get("code") or "STREAM_ERROR"),
                            str(data.get("message") or "问答失败，请稍后重试"),
                            int(data.get("status") or 0),
                        )
                    if event == "done":
                        result = data
                    if on_event is not None:
                        on_event(event, data)
        except httpx.TimeoutException as error:
            raise BackendError("BACKEND_TIMEOUT", "问答生成超时，请稍后重试", 0) from error
        except httpx.HTTPError as error:
            raise BackendError(
                "BACKEND_UNREACHABLE",
                "无法连接 RAG 服务：请确认后端已启动并处于本机演示模式",
                0,
            ) from error
        if not isinstance(result, dict):
            raise BackendError("STREAM_INTERRUPTED", "流式响应意外中断，请重试", 0)
        return result

    def chat_once(self, question: str, conversation_id: str, *,
                  top_k: int | None = None,
                  on_event: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Non-streaming fallback used when SSE is undesirable on this host."""
        payload = build_chat_payload(question, conversation_id,
                                     top_k=self.top_k if top_k is None else top_k)
        data = self._request("POST", "/chat/runs", json=payload)
        if not isinstance(data, dict):
            raise BackendError("INVALID_RESPONSE", "RAG 服务返回了无法解析的响应")
        if on_event is not None:
            on_event("done", data)
        return data

    def ask(self, question: str, conversation_id: str, *, stream: bool = True,
            top_k: int | None = None,
            on_event: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """One chat turn, streaming when requested and fallback otherwise."""
        if stream:
            return self.stream_chat(question, conversation_id, top_k=top_k, on_event=on_event)
        return self.chat_once(question, conversation_id, top_k=top_k, on_event=on_event)
