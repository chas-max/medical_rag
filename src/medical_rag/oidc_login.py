"""Experimental, isolated OIDC login application; never mounted on the legacy API.

This provides authorization-code + S256 PKCE, browser-bound one-use PostgreSQL
login transactions, ID-token verification, and opaque sessions. It is not a
production deployment; login alone is not patient authorization. An institution must
independently configure a real provider, approved MFA mapping, keys/rotation,
TLS and trusted proxy handling, request limits, database privileges and audit.
The signed provider-logout profile requires exact IdP registration/support.
Provider tokens are discarded; local account/role/patient
provisioning remains a separate trusted process. Existing endpoints/workers
stay locked. The returned session identity is a per-operation snapshot, not
permission to process patient content or a way to interrupt in-flight work.
Pending login state is institution-version-bound, but cannot bind an unknown
account's version before the provider reveals its subject. Account disable/
re-enable during a pending login still requires an institution-approved policy.
Signed provider fences prevent pre-logout authentication from reissuing local
authority; they do not interrupt in-flight work or push-clear idle browser UI.

The AES-GCM key must come from institutional secret management, not the DB or
clients. Expired login rows require a bounded operational cleanup policy. Do
not enable request/response body or callback query logging at any proxy/server;
in particular Uvicorn must run with access_log=False/--no-access-log. This app
does not configure or override external server/proxy loggers.
"""

import asyncio
import ipaddress
import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from secrets import token_bytes, token_urlsafe
from urllib.parse import urlsplit
from uuid import UUID

# Authlib >=1.9 speaks httpx2; mixing its client with an httpx (v1) transport
# fails inside httpx2 with a type assertion. Keep this module entirely on the
# same client stack as the OAuth integration.
import httpx2 as httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg import Connection
from psycopg.pq import TransactionStatus
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .access_policy import Principal
from .identity_sessions import AuthenticationDenied, IssuedSession, SessionStore
from .oidc_identity import InvalidIdentity, OIDCIdentityVerifier
from .provider_logout import (
    InvalidProviderLogout,
    ProviderLogoutVerifier,
    read_logout_token,
)
from .provider_tls import provider_ssl_context
from .security_audit import (
    AuditAction,
    AuditStore,
    AuditUnavailable,
    unavailable_response,
)
from .signing_keys import SigningKeys

_OPAQUE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_LOGIN_COOKIE = "__Host-medical-oidc"
_SESSION_COOKIE = "__Host-medical-session"
_CSRF_COOKIE = "__Host-medical-csrf"
_MAX_TOKEN_RESPONSE = 65536
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


class InvalidLogin(Exception):
    """Missing, expired, already consumed or wrong-browser login request."""


def _https_url(value: object):
    if (type(value) is not str or "?" in value or "#" in value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise ValueError("Invalid trusted OIDC configuration")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("Invalid trusted OIDC configuration")
        _ = parsed.port
        return parsed
    except (TypeError, ValueError):
        raise ValueError("Invalid trusted OIDC configuration") from None


@dataclass(frozen=True, slots=True)
class LoginConfiguration:
    """Fixed, trusted institutional settings, never derived from a request."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    callback_uri: str
    client_id: str
    client_secret: str = field(repr=False)
    public_key: RSAPublicKey | None = field(repr=False)
    mfa_acr_values: frozenset[str]
    max_authentication_age_seconds: int = 600
    provider_ca_file: Path | None = field(default=None, repr=False)
    verification_keys: Mapping[str, RSAPublicKey] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        keys = SigningKeys(self.public_key, self.verification_keys)
        object.__setattr__(self, "verification_keys", keys.verification_keys)
        issuer = _https_url(self.issuer)
        authorization = _https_url(self.authorization_endpoint)
        token = _https_url(self.token_endpoint)
        callback = _https_url(self.callback_uri)
        if (authorization.netloc != issuer.netloc or token.netloc != issuer.netloc
                or not authorization.path.startswith(issuer.path.rstrip("/") + "/")
                or not token.path.startswith(issuer.path.rstrip("/") + "/")
                or callback.path != "/auth/callback" or type(self.client_secret) is not str
                or not self.client_secret or any(ord(char) < 32 for char in self.client_secret)):
            raise ValueError("Invalid trusted OIDC configuration")
        OIDCIdentityVerifier(issuer=self.issuer, audience=self.client_id, public_key=self.public_key,
                             verification_keys=self.verification_keys,
                             mfa_acr_values=self.mfa_acr_values,
                             max_authentication_age_seconds=self.max_authentication_age_seconds)
        if self.provider_ca_file is not None:
            provider_ssl_context(self.provider_ca_file)

    @property
    def app_origin(self) -> str:
        parsed = urlsplit(self.callback_uri)
        return "https://" + parsed.netloc


@dataclass(frozen=True, slots=True)
class LoginChallenge:
    state: str = field(repr=False)
    browser_token: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ConsumedLogin:
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    tenant_version: int


def _hash(value: object) -> bytes:
    if type(value) is not str or _OPAQUE.fullmatch(value) is None:
        raise InvalidLogin()
    return sha256(value.encode("ascii")).digest()


class LoginTransactionStore:
    """Encrypt PKCE material and consume a browser-bound flow exactly once."""

    def __init__(self, connection_factory: ConnectionFactory, *, tenant_id: str,
                 encryption_key: bytes, lifetime_seconds: int = 300) -> None:
        try:
            self._tenant = UUID(tenant_id)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Invalid institution configuration") from None
        if (type(encryption_key) is not bytes or len(encryption_key) != 32
                or type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= 600):
            raise ValueError("Invalid login storage configuration")
        self._cipher = AESGCM(encryption_key)
        self._connection_factory = connection_factory
        self.lifetime_seconds = lifetime_seconds

    @contextmanager
    def _transaction(self):
        with self._connection_factory() as connection:
            if connection.info.transaction_status != TransactionStatus.IDLE:
                raise InvalidLogin()
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
                connection.execute("SET LOCAL search_path TO pg_catalog")
                connection.execute("SET LOCAL lock_timeout = '3s'")
                connection.execute("SET LOCAL statement_timeout = '5s'")
                if connection.execute("SELECT medical_rag.runtime_role_basic_check() "
                                      "AND NOT pg_catalog.pg_is_in_recovery()").fetchone() != (True,):
                    raise InvalidLogin()
                yield connection

    def create(self) -> LoginChallenge:
        challenge = LoginChallenge(token_urlsafe(32), token_urlsafe(32), token_urlsafe(32), token_urlsafe(64))
        digest = _hash(challenge.state)
        iv = token_bytes(12)
        payload = json.dumps({"nonce": challenge.nonce, "code_verifier": challenge.code_verifier}).encode("ascii")
        context = iv + self._cipher.encrypt(iv, payload, self._tenant.bytes + digest)
        with self._transaction() as connection:
            row = connection.execute(
                "INSERT INTO medical_rag.oidc_login_transactions "
                "(tenant_id,state_hash,browser_hash,encrypted_context,tenant_version,expires_at) "
                "SELECT t.tenant_id,%s,%s,%s,t.session_version,clock_timestamp()+(%s*interval '1 second') "
                "FROM medical_rag.tenants t WHERE t.tenant_id=%s AND t.active RETURNING state_hash",
                (digest, _hash(challenge.browser_token), context, self.lifetime_seconds, self._tenant),
            ).fetchone()
            if row is None:
                raise InvalidLogin()
        return challenge

    def consume(self, state: str, browser_token: str) -> ConsumedLogin:
        digest, browser = _hash(state), _hash(browser_token)
        with self._transaction() as connection:
            # Lock before evaluating time: a waiting statement must not extend
            # the usable life of a flow whose expiry passed while it waited.
            if connection.execute(
                "SELECT 1 FROM medical_rag.oidc_login_transactions "
                "WHERE tenant_id=%s AND state_hash=%s AND browser_hash=%s FOR UPDATE",
                (self._tenant, digest, browser),
            ).fetchone() is None:
                raise InvalidLogin()
            row = connection.execute(
                "DELETE FROM medical_rag.oidc_login_transactions f USING medical_rag.tenants t "
                "WHERE f.tenant_id=%s AND f.state_hash=%s AND f.browser_hash=%s "
                "AND f.expires_at>clock_timestamp() AND f.tenant_id=t.tenant_id AND t.active "
                "AND f.tenant_version=t.session_version "
                "RETURNING f.encrypted_context,f.tenant_version", (self._tenant, digest, browser),
            ).fetchone()
            if row is None:
                raise InvalidLogin()
        # The committed deletion remains final, including on decrypt/provider
        # failure. Neither a retry nor another worker can reuse this request.
        try:
            context = bytes(row[0])
            values = json.loads(self._cipher.decrypt(context[:12], context[12:], self._tenant.bytes + digest))
            nonce, verifier = values["nonce"], values["code_verifier"]
            _hash(nonce)
            if type(verifier) is not str or re.fullmatch(r"[A-Za-z0-9_-]{86}", verifier) is None:
                raise InvalidLogin()
            return ConsumedLogin(nonce, verifier, row[1])
        except (InvalidTag, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise InvalidLogin() from None


class _LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self._stream = stream

    async def __aiter__(self):
        length = 0
        async for part in self._stream:
            length += len(part)
            if length > _MAX_TOKEN_RESPONSE:
                raise InvalidLogin()
            yield part

    async def aclose(self) -> None:
        await self._stream.aclose()


class _LimitedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        # A parseable, signed ID token is not an OAuth success response by
        # itself. Reject HTTP errors/redirects before Authlib parses the body.
        if (response.status_code != 200
                or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
                or response.headers.get("content-encoding", "identity").lower() != "identity"
                or (response.is_stream_consumed and len(response.content) > _MAX_TOKEN_RESPONSE)):
            await response.aclose()
            raise InvalidLogin()
        response.stream = _LimitedStream(response.stream)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class OIDCLogin:
    """A confidential OIDC client, fixed to one institution and provider."""

    def __init__(self, config: LoginConfiguration, connection_factory: ConnectionFactory, *, tenant_id: str,
                 transaction_encryption_key: bytes, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if type(config) is not LoginConfiguration:
            raise ValueError("Trusted login configuration is required")
        self.config = config
        self.transactions = LoginTransactionStore(connection_factory, tenant_id=tenant_id,
                                                   encryption_key=transaction_encryption_key)
        self.sessions = SessionStore(connection_factory, tenant_id=tenant_id)
        self._verifier = OIDCIdentityVerifier(issuer=config.issuer, audience=config.client_id,
                                             verification_keys=config.verification_keys,
                                             public_key=config.public_key, mfa_acr_values=config.mfa_acr_values,
                                             max_authentication_age_seconds=config.max_authentication_age_seconds)
        self._test_transport = transport
        self.provider_logout_verifier = ProviderLogoutVerifier(issuer=config.issuer, audience=config.client_id,
                                                               verification_keys=config.verification_keys,
                                                               public_key=config.public_key)

    def _client(self) -> AsyncOAuth2Client:
        transport = self._test_transport or httpx.AsyncHTTPTransport(
            verify=provider_ssl_context(self.config.provider_ca_file), trust_env=False, retries=0)
        return AsyncOAuth2Client(
            client_id=self.config.client_id, client_secret=self.config.client_secret,
            token_endpoint_auth_method="client_secret_basic", redirect_uri=self.config.callback_uri,
            scope="openid", code_challenge_method="S256", follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
            trust_env=False, timeout=httpx.Timeout(5.0, connect=3.0), transport=_LimitedTransport(transport),
        )

    async def start(self) -> tuple[str, LoginChallenge]:
        challenge = await run_in_threadpool(self.transactions.create)
        async with self._client() as client:
            assurance_request = ({"acr_values": " ".join(sorted(self.config.mfa_acr_values))}
                                 if self.config.mfa_acr_values else {})
            url, _ = client.create_authorization_url(
                self.config.authorization_endpoint, state=challenge.state, nonce=challenge.nonce,
                code_verifier=challenge.code_verifier, max_age=self.config.max_authentication_age_seconds,
                **assurance_request,
            )
        return url, challenge

    async def finish(self, *, state: str, browser_token: str, code: str) -> IssuedSession:
        if type(code) is not str or not 0 < len(code) <= 2048 or any(not 33 <= ord(char) <= 126 for char in code):
            raise InvalidLogin()
        flow = await run_in_threadpool(self.transactions.consume, state, browser_token)
        try:
            async with asyncio.timeout(10), self._client() as client:
                token = await client.fetch_token(
                    self.config.token_endpoint, grant_type="authorization_code", code=code,
                    code_verifier=flow.code_verifier,
                )
            if not isinstance(token, dict):
                raise InvalidLogin()
            access_token, token_type, id_token = token.get("access_token"), token.get("token_type"), token.get("id_token")
            if (type(access_token) is not str or not 0 < len(access_token) <= 16384
                    or any(not 33 <= ord(char) <= 126 for char in access_token)
                    or type(token_type) is not str or token_type.lower() != "bearer"
                    or type(id_token) is not str or not 0 < len(id_token) <= 16384):
                raise InvalidLogin()
            identity = self._verifier.verify(id_token, expected_nonce=flow.nonce)
        except InvalidIdentity:
            raise InvalidLogin() from None
        except Exception:  # noqa: BLE001 - upstream error text is never public
            # OAuth/provider errors can include credentials and response bodies.
            raise InvalidLogin() from None
        return await run_in_threadpool(self.sessions.issue, identity, expected_tenant_version=flow.tenant_version)


def _cookie(request: Request, name: str) -> str:
    occurrences = sum(1 for header in request.headers.getlist("cookie") for part in header.split(";")
                      if part.partition("=")[0].strip() == name)
    value = request.cookies.get(name)
    if occurrences != 1 or not value:
        raise InvalidLogin()
    return value


def _failure(status: int) -> JSONResponse:
    return JSONResponse({"success": False, "data": None,
                         "error": {"code": "AUTHENTICATION_FAILED" if status != 503 else "AUTHENTICATION_UNAVAILABLE",
                                   "message": "身份验证失败" if status != 503 else "身份服务暂不可用"}},
                        status_code=status)


def _set_cookie(response, name: str, value: str, *, max_age: int | None = None) -> None:
    response.set_cookie(name, value, max_age=max_age, secure=True, httponly=True, samesite="lax", path="/")


def _clear_cookie(response, name: str) -> None:
    response.delete_cookie(name, secure=True, httponly=True, samesite="lax", path="/")


def _authorization_origin(endpoint: str) -> str:
    """Serialize one fixed HTTPS CSP source, rejecting source-list injection."""
    parsed = _https_url(endpoint)
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
        if getattr(address, "scope_id", None) is not None:
            raise ValueError("Invalid trusted OIDC configuration")
        authority = f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        if (not host or len(host) > 253 or any(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
                for label in host.split("."))):
            raise ValueError("Invalid trusted OIDC configuration") from None
        authority = host.lower()
    if parsed.port is not None:
        if not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid trusted OIDC configuration")
        authority += f":{parsed.port}"
    return "https://" + authority


def create_auth_app(login: OIDCLogin, *, patient_html: bool = False, audit: AuditStore | None = None) -> FastAPI:
    """Reuse auth protections; standalone headless no-audit is experimental only.

    The patient factory always supplies its mandatory trusted AuditStore.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    origin = login.config.app_origin
    authority = urlsplit(origin).netloc
    # Fixed trusted factory choice only; never derive this policy from a request.
    csp = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
           f"base-uri 'none'; form-action 'self' {_authorization_origin(login.config.authorization_endpoint)}; "
           "frame-ancestors 'none'" if patient_html
           else "default-src 'none'; frame-ancestors 'none'")

    @app.middleware("http")
    async def enforce_origin_and_headers(request: Request, call_next):
        hosts = request.headers.getlist("host")
        origins = request.headers.getlist("origin")
        provider_post = request.method == "POST" and request.url.path == "/auth/backchannel-logout"
        if (request.url.scheme != "https" or len(hosts) != 1 or hosts[0].lower() != authority.lower()
                or (request.method not in {"GET", "HEAD", "OPTIONS"} and origins != [origin]
                    and not (provider_post and not origins))
                or (origins and origins != [origin] and request.url.path != "/auth/callback")):
            response = _failure(403)
        else:
            try:
                response = await call_next(request)
            except Exception:  # noqa: BLE001 - prevent sensitive ASGI tracebacks
                # Catch before ServerErrorMiddleware can re-raise a storage or
                # provider exception to an ASGI server's traceback logger.
                response = _failure(503)
        response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": csp,
                                 "Cross-Origin-Resource-Policy": "same-origin"})
        return response

    @app.exception_handler(InvalidLogin)
    @app.exception_handler(AuthenticationDenied)
    async def deny(request: Request, error: Exception):
        response = _failure(401)
        if request.url.path == "/auth/callback":
            _clear_cookie(response, _LOGIN_COOKIE)
        return response

    @app.exception_handler(Exception)
    async def unavailable(request: Request, error: Exception):
        return _failure(503)

    @app.exception_handler(StarletteHTTPException)
    async def framework_error(request: Request, error: StarletteHTTPException):
        return _failure(error.status_code if error.status_code in {404, 405} else 400)

    @app.post("/auth/login")
    async def begin():
        url, challenge = await login.start()
        if audit is not None:
            await run_in_threadpool(audit.record, AuditAction.LOGIN_START)
        response = RedirectResponse(url, status_code=303)
        _set_cookie(response, _LOGIN_COOKIE, challenge.browser_token, max_age=login.transactions.lifetime_seconds)
        return response

    @app.post("/auth/backchannel-logout")
    async def provider_logout(request: Request):
        # Deliberately never read cookies or forwarded identity. Only the fixed
        # pinned-key signature supplies authority, not a local session or role.
        if audit is None:
            # Experimental headless auth must not enable unaudited provider
            # authority. The protected patient factory always supplies audit.
            return _failure(503)
        try:
            token = await read_logout_token(request)
            event = await run_in_threadpool(login.provider_logout_verifier.verify, token)
            await run_in_threadpool(login.sessions.provider_logout, event)
        except InvalidProviderLogout:
            return _failure(400)
        except Exception:  # noqa: BLE001 -- static storage failure, no token/SQL logging
            return _failure(503)
        await run_in_threadpool(audit.record, AuditAction.PROVIDER_LOGOUT)
        return JSONResponse({"success": True, "data": None, "error": None})

    @app.get("/auth/callback")
    async def callback(request: Request):
        pairs = request.query_params.multi_items()
        allowed = {"state", "code", "iss", "session_state"}
        if (any(key not in allowed for key, _ in pairs) or len({key for key, _ in pairs}) != len(pairs)
                or ("iss" in request.query_params and request.query_params["iss"] != login.config.issuer)):
            raise InvalidLogin()
        session = await login.finish(state=request.query_params.get("state"), browser_token=_cookie(request, _LOGIN_COOKIE),
                                     code=request.query_params.get("code"))
        try:
            # Finish replacing the previous session before recording a success.
            # A storage failure here must not orphan an unexposed new session.
            try:
                previous, csrf = _cookie(request, _SESSION_COOKIE), _cookie(request, _CSRF_COOKIE)
            except InvalidLogin:
                pass
            else:
                try:
                    await run_in_threadpool(login.sessions.revoke, previous, csrf_token=csrf)
                except AuthenticationDenied:
                    pass
            if audit is not None:
                # Current local authority, not submitted/provider claims. The
                # audit commits before either new browser cookie is emitted.
                def record_login() -> None:
                    with audit.operation(session.token, AuditAction.LOGIN_FINISH,
                                         mutation=True, csrf_token=session.csrf_token):
                        pass
                await run_in_threadpool(record_login)
        except Exception:  # revoke unexposed authority before propagating a static failure
            try:
                await run_in_threadpool(login.sessions.revoke, session.token, csrf_token=session.csrf_token)
            except Exception:  # noqa: BLE001,S110 -- best effort; logging could expose raw session/SQL
                pass
            if audit is not None:
                raise AuditUnavailable() from None
            raise
        response = RedirectResponse("/", status_code=303)
        _clear_cookie(response, _LOGIN_COOKIE)
        _set_cookie(response, _SESSION_COOKIE, session.token)
        _set_cookie(response, _CSRF_COOKIE, session.csrf_token)
        return response

    @app.get("/auth/session")
    async def session_info(request: Request):
        token, csrf = _cookie(request, _SESSION_COOKIE), _cookie(request, _CSRF_COOKIE)
        if audit is None:
            principal = await run_in_threadpool(login.sessions.authenticate, token, require_csrf=True, csrf_token=csrf)
        else:
            def read_session() -> Principal:
                with audit.operation(token, AuditAction.SESSION_READ, mutation=True, csrf_token=csrf) as authorized:
                    return authorized.principal
            principal = await run_in_threadpool(read_session)
        return {"success": True, "data": {"user_id": principal.user_id, "tenant_id": principal.tenant_id,
                                          "roles": sorted(principal.roles), "csrf_token": csrf}, "error": None}

    @app.post("/auth/logout")
    async def logout(request: Request):
        try:
            headers = request.headers.getlist("x-csrf-token")
            if len(headers) != 1:
                return _failure(403)
            token = _cookie(request, _SESSION_COOKIE)
            principal = await run_in_threadpool(login.sessions.authenticate, token, require_csrf=True, csrf_token=headers[0])
            await run_in_threadpool(login.sessions.revoke, token, csrf_token=headers[0])
        except (InvalidLogin, AuthenticationDenied):
            return _failure(403)
        response = JSONResponse({"success": True, "data": None, "error": None})
        if audit is not None:
            try:
                await run_in_threadpool(audit.record, AuditAction.LOGOUT, principal=principal)
            except AuditUnavailable:
                response = unavailable_response()
        for name in (_SESSION_COOKIE, _CSRF_COOKIE, _LOGIN_COOKIE):
            _clear_cookie(response, name)
        return response

    if audit is not None:
        audit.install(app)
    return app
