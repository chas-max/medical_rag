"""Typed chat entry points, independent of retrieval dependency initialization."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .public_errors import public_error
from .types import ApiError
from .workflow.contracts import StreamEmitter

if TYPE_CHECKING:
    from .chat_runtime import ChatRuntime

_STREAM_BUSINESS_ERRORS = frozenset({"REQUEST_ID_CONFLICT", "REQUEST_IN_PROGRESS"})
_UPSTREAM_EVENTS = frozenset({"chat.progress", "node.progress", "status", "claim", "analysis"})
_DEFAULT_PUBLIC_ERROR = public_error(None)
_STREAM_DEPENDENCY_ERROR = {**_DEFAULT_PUBLIC_ERROR.data(), "status": _DEFAULT_PUBLIC_ERROR.status}


def _public_stream_error(error: ApiError) -> dict:
    """Only explicitly known codes have public, fixed messages and status."""
    if type(error.code) is str and error.code in _STREAM_BUSINESS_ERRORS:
        safe_error = public_error(error.code)
        return {**safe_error.data(), "status": safe_error.status}
    return _STREAM_DEPENDENCY_ERROR.copy()


class ChatRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    inputs: dict[str, object]
    workflow_id: Literal["medical-answer"] = "medical-answer"
    memory_consent: bool = Field(default=False, strict=True)

    @field_validator("workflow_id", mode="before")
    @classmethod
    def default_workflow(cls, value: object) -> object:
        return "medical-answer" if value is None else value

    @field_validator("inputs")
    @classmethod
    def question_required(cls, value: dict[str, object]) -> dict[str, object]:
        question = value.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > 20000:
            raise ValueError("A bounded user question is required")
        return value


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    token: str = Field(min_length=1, max_length=256)
    accept: bool = Field(strict=True)
    memory_consent: bool = Field(default=False, strict=True)


def stream_response(execute: Callable[[StreamEmitter], dict],
                    event_frame: Callable[[dict], str]) -> StreamingResponse:
    """Bridge worker events; done is sent only after authoritative persistence."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    finished = object()
    terminal_lock = threading.Lock()
    terminal_sent = False

    def enqueue(event: dict, *, terminal: bool = False) -> None:
        nonlocal terminal_sent
        with terminal_lock:
            if terminal_sent:
                return
            terminal_sent = terminal
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def emit(event: dict) -> None:
        try:
            # The producer retains its object and can mutate it after this call.
            # Validate only the independent JSON snapshot that will be sent.
            if type(event) is not dict:
                raise ValueError("Invalid stream event")
            snapshot = json.loads(json.dumps(event, ensure_ascii=False, allow_nan=False))
            pending = [(snapshot, 0)]
            while pending:
                value, depth = pending.pop()
                if depth > 64:
                    raise ValueError("Stream event is too deeply nested")
                if isinstance(value, dict):
                    pending.extend((child, depth + 1) for child in value.values())
                elif isinstance(value, list):
                    pending.extend((child, depth + 1) for child in value)
            name = snapshot.get("event")
            if type(name) is not str or name not in _UPSTREAM_EVENTS:
                raise ValueError("Invalid stream event")
        except Exception:  # noqa: BLE001 - upstream data is untrusted
            enqueue({"event": "error", "data": _STREAM_DEPENDENCY_ERROR.copy()}, terminal=True)
            return
        enqueue(snapshot)

    def work() -> None:
        try:
            enqueue({"event": "done", "data": execute(emit)}, terminal=True)
        except ApiError as error:
            enqueue({"event": "error", "data": _public_stream_error(error)}, terminal=True)
        except Exception:  # noqa: BLE001 - never expose raw provider responses
            enqueue({"event": "error", "data": _STREAM_DEPENDENCY_ERROR.copy()}, terminal=True)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, finished)

    async def events() -> AsyncIterator[str]:
        loop.run_in_executor(None, work)
        while True:
            item = await queue.get()
            if item is finished:
                return
            yield event_frame(item)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def chat_router(runtime: ChatRuntime, success: Callable[[object], dict],
                event_frame: Callable[[dict], str]) -> APIRouter:
    router = APIRouter(prefix="/chat")

    @router.post("/runs")
    async def run(payload: ChatRunRequest) -> dict:
        return success(await run_in_threadpool(runtime.run, payload))

    @router.post("/runs/stream")
    async def stream(payload: ChatRunRequest) -> StreamingResponse:
        return stream_response(lambda emit: runtime.run(payload, emit), event_frame)

    @router.post("/confirmations")
    async def confirm(payload: ConfirmationRequest) -> dict:
        return success(await run_in_threadpool(runtime.confirm, payload))

    return router
