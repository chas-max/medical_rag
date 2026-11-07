"""Protected isolated OIDC/patient JSON API; no legacy or inference routes."""

import json
from typing import TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
)
from starlette.concurrency import run_in_threadpool

from .oidc_login import OIDCLogin, _cookie, create_auth_app
from .patient_chat import ChatUnavailable
from .patient_dialogue import ConversationContextChanged
from .patient_service import (
    ChatAnswerer,
    HealthNotice,
    JsonObject,
    PatientDenied,
    PatientService,
    ResourceMissing,
)
from .security_audit import AuditDenied
from .support_service import (
    SupportConflict,
    SupportDenied,
    SupportLimit,
    SupportService,
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


BodyT = TypeVar("BodyT", bound=_Body)


class _Grant(_Body):
    version: StrictStr = Field(min_length=1, max_length=128)
    accepted: StrictBool


class _Conversation(_Body):
    title: StrictStr = Field(min_length=1, max_length=200)


class _Turn(_Body):
    content: StrictStr = Field(min_length=1, max_length=16000)


class _Ask(_Body):
    question: StrictStr = Field(min_length=1, max_length=16000)
    conversation_id: StrictStr | None = None


class _SupportCreate(_Body):
    category: StrictStr = Field(min_length=1, max_length=32)


class _SupportResolve(_Body):
    resolution_code: StrictStr = Field(min_length=1, max_length=32)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "data": None, "error": {"code": code, "message": message}},
                        status_code=status)


def _ok(data: JsonObject) -> JsonObject:
    return {"success": True, "data": data, "error": None}


def _pagination(request: Request) -> dict[str, int]:
    pairs = request.query_params.multi_items()
    if any(key not in {"limit", "offset"} for key, _ in pairs) or len(pairs) != len(dict(pairs)):
        raise ValueError("Invalid pagination")
    try:
        return {"limit": int(request.query_params.get("limit", "20")),
                "offset": int(request.query_params.get("offset", "0"))}
    except (ValueError, TypeError):
        raise ValueError("Invalid pagination") from None


async def _body(request: Request, model: type[BodyT], *, allow_empty: bool = False) -> BodyT:
    # Bound bytes before parsing JSON, including chunked input. Never include
    # received input/validation objects in responses or application logs.
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 100000:
            raise ValueError("Invalid request")
        data.extend(chunk)
    try:
        def unique(pairs: list[tuple[str, object]]) -> JsonObject:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Invalid request")
                result[key] = value
            return result
        return model.model_validate({} if allow_empty and not data else json.loads(data, object_pairs_hook=unique))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("Invalid request") from None


def create_patient_app(login: OIDCLogin, notice: HealthNotice, *,
                       patient_html: bool = False,
                       chat: ChatAnswerer | None = None,
                       dialogue_state_enabled: bool = False) -> FastAPI:
    """Reuse all headless OIDC cookie, origin and no-store protections."""
    service = PatientService(login.sessions, notice, chat,
                             dialogue_state_enabled=dialogue_state_enabled)
    support = SupportService(login.sessions)
    app = create_auth_app(login, patient_html=patient_html, audit=service.audit)

    @app.exception_handler(ChatUnavailable)
    async def chat_unavailable(request: Request, error: ChatUnavailable) -> JSONResponse:
        return _error(503, "CHAT_UNAVAILABLE", "Question answering is unavailable")

    @app.exception_handler(ConversationContextChanged)
    async def context_changed(request: Request, error: ConversationContextChanged) -> JSONResponse:
        return _error(409, "CONVERSATION_CONTEXT_CHANGED", "Conversation changed, please resend the question")

    @app.exception_handler(SupportDenied)
    async def support_denied(request: Request, error: SupportDenied) -> JSONResponse:
        return _error(403, "SUPPORT_ACCESS_DENIED", "Support access denied")

    @app.exception_handler(SupportConflict)
    async def support_conflict(request: Request, error: SupportConflict) -> JSONResponse:
        return _error(409, "SUPPORT_CONFLICT", "Support transition unavailable")

    @app.exception_handler(SupportLimit)
    async def support_limit(request: Request, error: SupportLimit) -> JSONResponse:
        return _error(429, "SUPPORT_LIMIT", "Support admission limit reached")

    @app.exception_handler(AuditDenied)
    async def audit_denied(request: Request, error: AuditDenied) -> JSONResponse:
        return _error(403, "AUDIT_ACCESS_DENIED", "Audit access denied")

    @app.exception_handler(PatientDenied)
    async def denied(request: Request, error: PatientDenied) -> JSONResponse:
        return _error(403, "PATIENT_ACCESS_DENIED", "Patient access denied")

    @app.exception_handler(ResourceMissing)
    async def missing(request: Request, error: ResourceMissing) -> JSONResponse:
        return _error(404, "RESOURCE_NOT_FOUND", "Resource not found")

    @app.exception_handler(ValueError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, error: Exception) -> JSONResponse:
        return _error(400, "INVALID_REQUEST", "Invalid request")

    async def credentials(request: Request, *, mutation: bool = False) -> tuple[str, str | None]:
        token = _cookie(request, "__Host-medical-session")
        csrf = None
        if mutation:
            values = request.headers.getlist("x-csrf-token")
            if len(values) != 1:
                raise PatientDenied()
            csrf = values[0]
        # Authenticate BEFORE body/path/pagination validation. This principal
        # is discarded; the service obtains live authority in its own locked
        # operation transaction, never trusting a disconnected snapshot here.
        await run_in_threadpool(login.sessions.authenticate, token, require_csrf=mutation, csrf_token=csrf)
        service.audit.remember(token)
        return token, csrf

    @app.get("/audit")
    async def audit_events(request: Request) -> JsonObject:
        token, _ = await credentials(request)
        return _ok(await run_in_threadpool(service.audit.list_events, token, **_pagination(request)))

    @app.get("/me")
    async def me(request: Request) -> JsonObject:
        token, _ = await credentials(request)
        return _ok(await run_in_threadpool(service.me, token))

    @app.get("/me/consents/health")
    async def consent_status(request: Request) -> JsonObject:
        token, _ = await credentials(request)
        return _ok(await run_in_threadpool(service.consent_status, token))

    @app.post("/me/consents/health")
    async def grant(request: Request) -> JsonObject:
        token, csrf = await credentials(request, mutation=True)
        body = await _body(request, _Grant)
        if body.accepted is not True:
            raise ValueError("Explicit acceptance required")
        return _ok(await run_in_threadpool(service.change_consent, token, csrf_token=csrf,
                                           granted=True, version=body.version))

    @app.delete("/me/consents/health")
    async def withdraw(request: Request) -> JsonObject:
        token, csrf = await credentials(request, mutation=True)
        return _ok(await run_in_threadpool(service.change_consent, token, csrf_token=csrf, granted=False))

    @app.get("/conversations")
    async def conversations(request: Request) -> JsonObject:
        token, _ = await credentials(request)
        return _ok(await run_in_threadpool(service.list_conversations, token, **_pagination(request)))

    @app.post("/conversations")
    async def create(request: Request) -> JsonObject:
        token, csrf = await credentials(request, mutation=True)
        body = await _body(request, _Conversation)
        return _ok(await run_in_threadpool(service.create_conversation, token, csrf_token=csrf, title=body.title))

    @app.get("/conversations/{identifier}")
    async def conversation(request: Request, identifier: str) -> JsonObject:
        token, _ = await credentials(request)
        return _ok(await run_in_threadpool(service.get_conversation, token, identifier))

    @app.get("/conversations/{identifier}/turns")
    async def turns(request: Request, identifier: str) -> JsonObject:
        token, _ = await credentials(request)
        return _ok(await run_in_threadpool(service.list_turns, token, identifier, **_pagination(request)))

    @app.post("/conversations/{identifier}/turns")
    async def append(request: Request, identifier: str) -> JsonObject:
        token, csrf = await credentials(request, mutation=True)
        body = await _body(request, _Turn)
        return _ok(await run_in_threadpool(service.append_turn, token, identifier,
                                           csrf_token=csrf, content=body.content))

    @app.post("/chat")
    async def ask(request: Request) -> JsonObject:
        token, csrf = await credentials(request, mutation=True)
        if chat is None:
            raise ChatUnavailable()
        body = await _body(request, _Ask)
        return _ok(await run_in_threadpool(service.ask, token, csrf_token=csrf,
                                           conversation_id=body.conversation_id,
                                           question=body.question))

    @app.post("/support/cases")
    async def support_create(request: Request) -> JsonObject:
        token, csrf = await credentials(request, mutation=True)
        body = await _body(request, _SupportCreate)
        return _ok(await run_in_threadpool(support.create, token, csrf_token=csrf, category=body.category))

    @app.get("/support/cases")
    async def support_list(request: Request) -> JsonObject:
        token, _ = await credentials(request)
        return _ok(await run_in_threadpool(support.list, token, **_pagination(request)))

    @app.get("/support/cases/{identifier}")
    async def support_read(request: Request, identifier: str) -> JsonObject:
        token, _ = await credentials(request)
        return _ok(await run_in_threadpool(support.get, token, identifier))

    @app.delete("/support/cases/{identifier}")
    async def support_cancel(request: Request, identifier: str) -> JsonObject:
        token, csrf = await credentials(request, mutation=True)
        await _body(request, _Body, allow_empty=True)
        return _ok(await run_in_threadpool(support.transition, token, identifier, csrf_token=csrf, operation="cancel"))

    @app.get("/support/queue")
    async def support_queue(request: Request) -> JsonObject:
        token, _ = await credentials(request)
        return _ok(await run_in_threadpool(support.list, token, queue=True, **_pagination(request)))

    @app.post("/support/queue/{identifier}/take")
    async def support_take(request: Request, identifier: str) -> JsonObject:
        token, csrf = await credentials(request, mutation=True)
        await _body(request, _Body)
        return _ok(await run_in_threadpool(support.transition, token, identifier, csrf_token=csrf, operation="take"))

    @app.post("/support/queue/{identifier}/resolve")
    async def support_resolve(request: Request, identifier: str) -> JsonObject:
        token, csrf = await credentials(request, mutation=True)
        body = await _body(request, _SupportResolve)
        return _ok(await run_in_threadpool(support.transition, token, identifier, csrf_token=csrf,
                                          operation="resolve", resolution_code=body.resolution_code))

    return app
