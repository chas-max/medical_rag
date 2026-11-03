"""Mandatory minimal audit for the isolated patient app, not legacy/headless auth.

Success uses the operation's live primary transaction; attempted denials use a
separate bounded transaction after rollback. Never pass exceptions, request
payloads, paths, external identities or resource identifiers into the recorder.
RLS is defense in depth under a trusted application, not arbitrary-SQL defense
or protection against a database/schema owner changing these protections.
"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from psycopg import Connection
from starlette.concurrency import run_in_threadpool

from .access_policy import Action, Principal, Resource, is_allowed
from .identity_sessions import AuthenticationDenied, AuthorizedTransaction, SessionStore


class AuditUnavailable(Exception):
    """Static public failure; never include storage error text."""


class AuditDenied(Exception):
    """Current principal lacks institution audit permission and verified MFA."""


class AuditAction(StrEnum):
    LOGIN_START = "login.start"
    LOGIN_FINISH = "login.finish"
    SESSION_READ = "session.read"
    LOGOUT = "logout"
    PROVIDER_LOGOUT = "provider.logout"
    IDENTITY_READ = "identity.read"
    CONSENT_READ = "consent.read"
    CONSENT_GRANT = "consent.grant"
    CONSENT_WITHDRAW = "consent.withdraw"
    CONVERSATION_CREATE = "conversation.create"
    CONVERSATION_LIST = "conversation.list"
    CONVERSATION_READ = "conversation.read"
    TURN_APPEND = "turn.append"
    TURN_LIST = "turn.list"
    AUDIT_READ = "audit.read"
    SUPPORT_CREATE = "support.create"
    SUPPORT_LIST = "support.list"
    SUPPORT_READ = "support.read"
    SUPPORT_CANCEL = "support.cancel"
    SUPPORT_QUEUE = "support.queue"
    SUPPORT_TAKE = "support.take"
    SUPPORT_RESOLVE = "support.resolve"
    REQUEST = "request"


class AuditResource(StrEnum):
    AUTHENTICATION = "authentication"
    IDENTITY = "identity"
    HEALTH_CONSENT = "health_consent"
    CONVERSATION = "conversation"
    TURN = "turn"
    AUDIT = "audit"
    APPLICATION = "application"
    SESSION = "session"
    SUPPORT_CASE = "support_case"


_RESOURCES = {
    **dict.fromkeys((AuditAction.SUPPORT_CREATE, AuditAction.SUPPORT_LIST, AuditAction.SUPPORT_READ,
                     AuditAction.SUPPORT_CANCEL, AuditAction.SUPPORT_QUEUE, AuditAction.SUPPORT_TAKE,
                     AuditAction.SUPPORT_RESOLVE), AuditResource.SUPPORT_CASE),
    AuditAction.PROVIDER_LOGOUT: AuditResource.SESSION,
    **dict.fromkeys((AuditAction.LOGIN_START, AuditAction.LOGIN_FINISH, AuditAction.SESSION_READ,
                     AuditAction.LOGOUT), AuditResource.AUTHENTICATION),
    AuditAction.IDENTITY_READ: AuditResource.IDENTITY,
    **dict.fromkeys((AuditAction.CONSENT_READ, AuditAction.CONSENT_GRANT,
                     AuditAction.CONSENT_WITHDRAW), AuditResource.HEALTH_CONSENT),
    **dict.fromkeys((AuditAction.CONVERSATION_CREATE, AuditAction.CONVERSATION_LIST,
                     AuditAction.CONVERSATION_READ), AuditResource.CONVERSATION),
    AuditAction.TURN_APPEND: AuditResource.TURN, AuditAction.TURN_LIST: AuditResource.TURN,
    AuditAction.AUDIT_READ: AuditResource.AUDIT, AuditAction.REQUEST: AuditResource.APPLICATION,
}


@dataclass(slots=True)
class _RequestAudit:
    action: AuditAction
    request_id: UUID = field(default_factory=uuid4)
    token: str | None = field(default=None, repr=False)
    committed: bool = False
    failed: bool = False


_request: ContextVar[_RequestAudit | None] = ContextVar("patient_audit_request", default=None)


def _action(request: Request) -> AuditAction | None:
    """Classify to fixed enums only; paths and IDs never enter audit storage."""
    path, method = request.url.path, request.method
    if path == "/" or path.startswith("/assets/"):
        return None
    fixed = {
        ("POST", "/auth/login"): AuditAction.LOGIN_START,
        ("GET", "/auth/callback"): AuditAction.LOGIN_FINISH,
        ("GET", "/auth/session"): AuditAction.SESSION_READ,
        ("POST", "/auth/logout"): AuditAction.LOGOUT,
        ("POST", "/auth/backchannel-logout"): AuditAction.PROVIDER_LOGOUT,
        ("GET", "/me"): AuditAction.IDENTITY_READ,
        ("GET", "/me/consents/health"): AuditAction.CONSENT_READ,
        ("POST", "/me/consents/health"): AuditAction.CONSENT_GRANT,
        ("DELETE", "/me/consents/health"): AuditAction.CONSENT_WITHDRAW,
        ("GET", "/conversations"): AuditAction.CONVERSATION_LIST,
        ("POST", "/conversations"): AuditAction.CONVERSATION_CREATE,
        ("GET", "/audit"): AuditAction.AUDIT_READ,
        ("POST", "/support/cases"): AuditAction.SUPPORT_CREATE,
        ("GET", "/support/cases"): AuditAction.SUPPORT_LIST,
        ("GET", "/support/queue"): AuditAction.SUPPORT_QUEUE,
    }
    if (method, path) in fixed:
        return fixed[method, path]
    parts = path.split("/")
    if len(parts) == 4 and parts[1:3] == ["support", "cases"]:
        if method == "GET":
            return AuditAction.SUPPORT_READ
        if method == "DELETE":
            return AuditAction.SUPPORT_CANCEL
    if len(parts) == 5 and parts[1:3] == ["support", "queue"] and method == "POST":
        if parts[4] == "take":
            return AuditAction.SUPPORT_TAKE
        if parts[4] == "resolve":
            return AuditAction.SUPPORT_RESOLVE
    if len(parts) == 3 and parts[1] == "conversations" and method == "GET":
        return AuditAction.CONVERSATION_READ
    if len(parts) == 4 and parts[1] == "conversations" and parts[3] == "turns":
        if method == "GET":
            return AuditAction.TURN_LIST
        if method == "POST":
            return AuditAction.TURN_APPEND
    return AuditAction.REQUEST


def unavailable_response() -> JSONResponse:
    return JSONResponse({"success": False, "data": None,
                         "error": {"code": "AUDIT_UNAVAILABLE", "message": "Security audit unavailable"}},
                        status_code=503)


def _closed_response(response: Response) -> JSONResponse:
    replacement = unavailable_response()
    # An already revoked logout must keep cookie deletions; never propagate
    # new login/session cookies or a redirect from an unrecorded success.
    for value in response.headers.getlist("set-cookie"):
        if "Max-Age=0" in value:
            replacement.headers.append("set-cookie", value)
    for key in ("cache-control", "pragma", "referrer-policy", "x-content-type-options",
                "x-frame-options", "content-security-policy", "cross-origin-resource-policy"):
        if key in response.headers:
            replacement.headers[key] = response.headers[key]
    return replacement


class AuditStore:
    """Trusted server institution, current SessionStore authority, no opt-out."""

    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions
        # Same trusted fixed configuration as session authority, never request IDs.
        self._tenant = sessions._tenant_id

    @staticmethod
    def remember(token: str) -> None:
        """Called only AFTER successful live authentication (including CSRF)."""
        if context := _request.get():
            context.token = token

    def _scope(self, db: Connection) -> None:
        db.execute("SELECT set_config('app.audit_scope',pg_current_xact_id()::text||'/'||%s,true)",
                   (str(self._tenant),))

    def _insert(self, db: Connection, action: AuditAction, *, principal: Principal | None = None,
                outcome: str = "allowed", reason: str = "ok") -> None:
        if not isinstance(action, AuditAction) or (principal and principal.tenant_id != str(self._tenant)):
            raise AuditUnavailable()
        context = _request.get()
        try:
            self._scope(db)
            db.execute("INSERT INTO medical_rag.security_audit_events "
                       "(tenant_id,operator_id,action,resource_type,outcome,reason_code,request_id) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (self._tenant, principal.user_id if principal else None, action, _RESOURCES[action],
                        outcome, reason, context.request_id if context else uuid4()))
        except Exception:  # noqa: BLE001 -- don't preserve or log sensitive SQL errors
            if context:
                context.failed = True
            raise AuditUnavailable() from None

    @staticmethod
    def _committed() -> None:
        if context := _request.get():
            context.committed = True

    @contextmanager
    def operation(self, token: str, action: AuditAction, *, mutation: bool = False,
                  csrf_token: str | None = None) -> Iterator[AuthorizedTransaction]:
        """Insert before exiting the SAME transaction, mark only after commit."""
        with self.sessions.authorized_transaction(token, require_csrf=mutation, csrf_token=csrf_token) as authorized:
            self.remember(token)
            yield authorized
            self._insert(authorized.connection, action, principal=authorized.principal)
        self._committed()

    def record(self, action: AuditAction, *, principal: Principal | None = None) -> None:
        """Auth lifecycle event after a verified result, before any cookie output."""
        try:
            with self.sessions._transaction() as db:
                self._insert(db, action, principal=principal)
        except Exception:  # noqa: BLE001 -- static boundary, including transaction/commit failure
            if context := _request.get():
                context.failed = True
            raise AuditUnavailable() from None
        self._committed()

    def record_failure(self, status: int) -> None:
        """Called after failed operation rollback. Recheck any earlier actor live."""
        context = _request.get()
        if context is None or context.failed:
            raise AuditUnavailable()
        reason = {400: "invalid_request", 401: "authentication_denied", 403: "access_denied",
                  404: "resource_not_found"}.get(status, "request_denied" if status < 500 else "operation_unavailable")
        outcome = "denied" if status < 500 else "error"
        try:
            if context.token:
                try:
                    with self.sessions.authorized_transaction(context.token) as authorized:
                        self._insert(authorized.connection, context.action, principal=authorized.principal,
                                     outcome=outcome, reason=reason)
                    self._committed()
                    return
                except AuthenticationDenied:
                    pass
            with self.sessions._transaction() as db:
                self._insert(db, context.action, outcome=outcome, reason=reason)
        except Exception:  # noqa: BLE001 -- no raw exception values beyond this boundary
            context.failed = True
            raise AuditUnavailable() from None
        self._committed()

    def list_events(self, token: str, *, limit: int = 20, offset: int = 0) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("Invalid pagination")
        with self.operation(token, AuditAction.AUDIT_READ) as authorized:
            principal = authorized.principal
            if not is_allowed(principal, Action.AUDIT_READ, Resource(principal.tenant_id)):
                raise AuditDenied()
            db = authorized.connection
            self._scope(db)
            rows = db.execute("SELECT event_id,recorded_at,operator_id,action,resource_type,outcome,reason_code,request_id "
                              "FROM medical_rag.security_audit_events WHERE tenant_id=%s "
                              "ORDER BY event_order DESC LIMIT %s OFFSET %s", (self._tenant, limit, offset)).fetchall()
            keys = ("id", "time", "operator_id", "action", "resource_type", "outcome", "reason_code", "request_id")
            return {"items": [dict(zip(keys, (str(row[0]), row[1].isoformat(), str(row[2]) if row[2] else None,
                                             *row[3:7], str(row[7])), strict=True)) for row in rows],
                    "limit": limit, "offset": offset}

    def install(self, app: FastAPI) -> None:
        """Outer guard sees origin/framework denials before response emission."""
        @app.middleware("http")
        async def mandatory_audit(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            action = _action(request)
            context = _RequestAudit(action or AuditAction.REQUEST)
            marker = _request.set(context)
            try:
                response = await call_next(request)
                if action is not None and not context.committed:
                    if response.status_code < 400:
                        response = _closed_response(response)
                    try:
                        await run_in_threadpool(self.record_failure, response.status_code)
                    except AuditUnavailable:
                        response = _closed_response(response)
                response.headers["X-Request-ID"] = str(context.request_id)
                return response
            finally:
                _request.reset(marker)
