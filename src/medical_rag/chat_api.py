"""Typed chat entry points, independent of retrieval dependency initialization."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .public_errors import public_error
from .sse import StreamEmitter, bridge
from .types import ApiError

if TYPE_CHECKING:
    from .chat_runtime import ChatRuntime

_STREAM_BUSINESS_ERRORS = frozenset({"REQUEST_ID_CONFLICT", "REQUEST_IN_PROGRESS",
                                    "CONVERSATION_CONTEXT_CHANGED"})
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


def _stream_failure(error: Exception) -> dict:
    """Only explicitly known codes have public, fixed messages and status."""
    return (_public_stream_error(error) if isinstance(error, ApiError)
            else _STREAM_DEPENDENCY_ERROR.copy())


def stream_response(execute: Callable[[StreamEmitter], dict],
                    event_frame: Callable[[dict], str]) -> StreamingResponse:
    """Bridge worker events; done is sent only after authoritative persistence."""
    return bridge(execute, _UPSTREAM_EVENTS, failure=_stream_failure, frame=event_frame)


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
