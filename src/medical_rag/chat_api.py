"""Typed chat entry points, independent of retrieval dependency initialization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .types import ApiError
from .workflow.contracts import StreamEmitter

if TYPE_CHECKING:
    from .chat_runtime import ChatRuntime


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

    def emit(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def work() -> None:
        try:
            emit({"event": "done", "data": execute(emit)})
        except ApiError as error:
            emit({"event": "error", "data": {"code": error.code, "message": error.message, "status": error.status}})
        except Exception:  # noqa: BLE001 - never expose raw provider responses
            emit({"event": "error", "data": {"code": "DEPENDENCY_ERROR", "message": "聊天服务暂不可用", "status": 503}})
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
