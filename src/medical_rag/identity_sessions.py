"""Experimental opaque sessions for isolated OIDC/patient APIs, not production.

Only a trusted login flow may pass an independently verified identity to issue;
constructing VerifiedIdentity is not authentication. The caller must configure a
single institutional tenant and connect to its authoritative primary database.
Role and binding administration, identity proofing, audit, transport security,
Legacy APIs/workers remain disconnected and locked. The isolated API supplies
route CSRF checks; PostgreSQL privilege review is still a deployment obligation.
The runtime database role can create or change session rows and is fully trusted
for session integrity; it must not be allowed to change authority tables or
execute arbitrary SQL on behalf of users. The partial diagnostic is not an audit.
After locking the session row, authorization uses a fresh READ COMMITTED SQL
statement. Revocations committed before that statement starts are observed;
changes committing after its snapshot are in flight and can miss this check.
The returned principal is a snapshot and must not be cached across operations;
this does not stop an already authorized operation, stream, or worker.
"""

import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from psycopg import Connection
from psycopg.pq import TransactionStatus

from .access_policy import Principal, Role
from .oidc_identity import VerifiedIdentity
from .provider_logout import (
    InvalidProviderLogout,
    VerifiedProviderLogout,
    valid_identifier,
)

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


class AuthenticationDenied(Exception):
    """Missing, invalid, expired, disabled or revoked session."""


class UnsafeConnection(Exception):
    """Connection cannot provide an independent authoritative transaction."""


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Raw values are returned once and excluded from object representations."""

    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthorizedTransaction:
    """Internal operation scope; valid only inside authorized_transaction.

    The session row stays locked until the operation commits or rolls back.
    Call refresh after waiting for a patient lock, before using any patient
    data. It rechecks time and live authority on this same primary connection
    and rejects changed authority. Never retain this object across operations.
    """

    connection: Connection = field(repr=False)
    principal: Principal
    _refresh: Callable[[], Principal] = field(repr=False)

    def refresh(self) -> Principal:
        current = self._refresh()
        if current != self.principal:
            raise AuthenticationDenied()
        return current


def _hash_token(value: object) -> bytes:
    if type(value) is not str or _TOKEN.fullmatch(value) is None:
        raise AuthenticationDenied()
    return sha256(value.encode("ascii")).digest()


class SessionStore:
    """Use a fresh database transaction to check current authority per call."""

    def __init__(self, connection_factory: ConnectionFactory, *, tenant_id: str,
                 absolute_lifetime_seconds: int = 3600, idle_lifetime_seconds: int = 600) -> None:
        try:
            self._tenant_id = UUID(tenant_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid institution configuration") from None
        if (type(absolute_lifetime_seconds) is not int or type(idle_lifetime_seconds) is not int
                or not 1 <= idle_lifetime_seconds <= absolute_lifetime_seconds <= 86400):
            raise ValueError("Invalid session lifetime configuration")
        self._connection_factory = connection_factory
        self._absolute = absolute_lifetime_seconds
        self._idle = idle_lifetime_seconds

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        with self._connection_factory() as connection:
            if connection.info.transaction_status != TransactionStatus.IDLE:
                raise UnsafeConnection()
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
                connection.execute("SET LOCAL search_path TO pg_catalog")
                connection.execute("SET LOCAL lock_timeout = '3s'")
                connection.execute("SET LOCAL statement_timeout = '5s'")
                if connection.execute("SELECT medical_rag.runtime_role_basic_check() "
                                      "AND NOT pg_catalog.pg_is_in_recovery()").fetchone() != (True,):
                    raise UnsafeConnection()
                # Schema6 is mandatory even for headless trusted fixtures.
                connection.execute("SELECT authenticated_at,provider_sid_hash FROM medical_rag.identity_sessions LIMIT 0")
                connection.execute("SELECT event_id,tenant_id,issuer,jti_hash,subject_hash,sid_hash,issued_at,expires_at,recorded_at "
                                   "FROM medical_rag.provider_logout_events LIMIT 0")
                yield connection

    def _provider_serialization(self, connection: Connection) -> None:
        """Shared transaction lock BEFORE any fence/session issue statement.

        A deterministic signed64-bit institution key is shared across processes;
        hash collisions only coarsen serialization, never remove protection.
        """
        key = int.from_bytes(sha256(b"medical-rag/provider-logout/" + self._tenant_id.bytes).digest()[:8],
                             "big", signed=True)
        connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (key,))
        connection.execute("SELECT set_config('app.provider_logout_scope',pg_current_xact_id()::text||'/'||%s,true)",
                           (str(self._tenant_id),))

    def issue(self, identity: VerifiedIdentity, *, expected_tenant_version: int | None = None) -> IssuedSession:
        """Issue fresh values; optionally bind a trusted pending-login epoch.

        The optional version is taken from server-side login state, never a
        client field. Comparing it inside the INSERT prevents a pending login
        from surviving institution disable/re-enable during an IdP exchange.
        """
        if (type(identity) is not VerifiedIdentity or type(identity.issuer) is not str
                or type(identity.subject) is not str or type(identity.mfa_verified) is not bool
                or not valid_identifier(identity.subject)
                or (identity.provider_session_id is not None and not valid_identifier(identity.provider_session_id))
                or (identity.authenticated_at is not None and (type(identity.authenticated_at) is not int
                                                              or not 0 <= identity.authenticated_at < 2**63))
                or (expected_tenant_version is not None and
                    (type(expected_tenant_version) is not int or not 0 <= expected_tenant_version < 2**63))):
            raise AuthenticationDenied()
        token, csrf_token = token_urlsafe(32), token_urlsafe(32)
        sid_hash = sha256(identity.provider_session_id.encode("utf-8")).digest() if identity.provider_session_id else None
        with self._transaction() as connection:
            self._provider_serialization(connection)
            # Fresh statement AFTER waiting for the institution lock: no stale
            # snapshot can resurrect a pending pre-logout code exchange.
            if connection.execute(
                "SELECT 1 FROM medical_rag.provider_logout_events WHERE tenant_id=%s AND issuer=%s "
                "AND (subject_hash IS NULL OR subject_hash=%s) AND (sid_hash IS NULL OR sid_hash=%s) "
                "AND (%s::bigint IS NULL OR issued_at >= %s) LIMIT 1",
                (self._tenant_id, identity.issuer, sha256(identity.subject.encode("utf-8")).digest(),
                 sid_hash, identity.authenticated_at, identity.authenticated_at),
            ).fetchone():
                raise AuthenticationDenied()
            row = connection.execute(
                "INSERT INTO medical_rag.identity_sessions "
                "(token_hash, csrf_hash, tenant_id, account_id, mfa_verified, "
                "account_version, tenant_version, expires_at, authenticated_at, provider_sid_hash) "
                "SELECT %s, %s, a.tenant_id, a.account_id, %s, a.session_version, t.session_version, "
                "clock_timestamp() + (%s * interval '1 second'), %s, %s "
                "FROM medical_rag.identity_accounts a JOIN medical_rag.tenants t USING (tenant_id) "
                "WHERE a.tenant_id=%s AND a.issuer=%s AND a.subject=%s AND a.active AND t.active "
                "AND (%s::bigint IS NULL OR t.session_version=%s) "
                "RETURNING account_id",
                (_hash_token(token), _hash_token(csrf_token), identity.mfa_verified, self._absolute,
                 identity.authenticated_at, sid_hash,
                 self._tenant_id, identity.issuer, identity.subject, expected_tenant_version, expected_tenant_version),
            ).fetchone()
            if row is None:
                raise AuthenticationDenied()
        return IssuedSession(token, csrf_token)

    def provider_logout(self, event: VerifiedProviderLogout) -> None:
        """Atomically append a fence and revoke, independent of local roles/MFA.

        Identical replay returns before UPDATE. Audit is a separate subsequent
        transaction so audit outages cannot undo provider revocation or fences.
        """
        if type(event) is not VerifiedProviderLogout:
            raise InvalidProviderLogout()
        jti = sha256(event.jti.encode("utf-8")).digest()
        subject = sha256(event.subject.encode("utf-8")).digest() if event.subject is not None else None
        sid = sha256(event.sid.encode("utf-8")).digest() if event.sid is not None else None
        with self._transaction() as db:
            self._provider_serialization(db)
            previous = db.execute(
                "SELECT subject_hash,sid_hash,issued_at,expires_at FROM medical_rag.provider_logout_events "
                "WHERE tenant_id=%s AND issuer=%s AND jti_hash=%s", (self._tenant_id, event.issuer, jti),
            ).fetchone()
            if previous is not None:
                if previous != (subject, sid, event.issued_at, event.expires_at):
                    raise InvalidProviderLogout()
                return
            db.execute("INSERT INTO medical_rag.provider_logout_events "
                       "(tenant_id,issuer,jti_hash,subject_hash,sid_hash,issued_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (self._tenant_id, event.issuer, jti, subject, sid, event.issued_at, event.expires_at))
            db.execute("UPDATE medical_rag.identity_sessions s SET revoked=true "
                       "FROM medical_rag.identity_accounts a WHERE s.tenant_id=%s AND a.tenant_id=s.tenant_id "
                       "AND s.account_id=a.account_id AND a.issuer=%s AND (%s::text IS NULL OR a.subject=%s) "
                       "AND (%s::bytea IS NULL OR s.provider_sid_hash=%s) "
                       "AND (s.authenticated_at IS NULL OR s.authenticated_at<=%s)",
                       (self._tenant_id, event.issuer, event.subject, event.subject, sid, sid, event.issued_at))

    def authenticate(self, token: str, *, require_csrf: bool = False,
                     csrf_token: str | None = None) -> Principal:
        """Read live account, institution, roles and verified binding in one statement."""
        with self.authorized_transaction(token, require_csrf=require_csrf, csrf_token=csrf_token) as authorized:
            return authorized.principal

    @contextmanager
    def authorized_transaction(self, token: str, *, require_csrf: bool = False,
                               csrf_token: str | None = None) -> Iterator[AuthorizedTransaction]:
        """Hold an independent READ COMMITTED session lock for one operation.

        Only trusted services may use the connection. Request fields cannot
        supply scope or authority. Refresh after patient-lock waits; an error
        rolls back all operation writes, including last-seen/session updates.
        """
        if type(require_csrf) is not bool:
            raise AuthenticationDenied()
        digest = _hash_token(token)
        csrf_digest = _hash_token(csrf_token) if require_csrf else None
        with self._transaction() as connection:
            # UPDATE's predicate may be evaluated before waiting for a row lock.
            # Lock first, then check time and current authority in a new READ
            # COMMITTED statement, so waiting cannot extend an expired session.
            if connection.execute(
                "SELECT 1 FROM medical_rag.identity_sessions "
                "WHERE tenant_id=%s AND token_hash=%s FOR UPDATE",
                (self._tenant_id, digest),
            ).fetchone() is None:
                raise AuthenticationDenied()
            principal = self._authorize(connection, digest, require_csrf, csrf_digest)
            yield AuthorizedTransaction(connection, principal,
                                        lambda: self._authorize(connection, digest, require_csrf, csrf_digest))

    def _authorize(self, connection: Connection, digest: bytes,
                   require_csrf: bool, csrf_digest: bytes | None) -> Principal:
        row = connection.execute(
                "WITH live AS (UPDATE medical_rag.identity_sessions s SET last_seen_at=clock_timestamp() "
                "FROM medical_rag.identity_accounts a JOIN medical_rag.tenants t USING (tenant_id) "
                "WHERE s.tenant_id=%s AND s.token_hash=%s AND s.tenant_id=a.tenant_id "
                "AND s.account_id=a.account_id AND NOT s.revoked AND a.active AND t.active "
                "AND s.account_version=a.session_version AND s.tenant_version=t.session_version "
                "AND s.expires_at > clock_timestamp() "
                "AND s.last_seen_at > clock_timestamp() - (%s * interval '1 second') "
                "AND (NOT %s OR s.csrf_hash=%s) RETURNING s.account_id, s.tenant_id, s.mfa_verified) "
                "SELECT l.account_id, l.mfa_verified, "
                "ARRAY(SELECT r.role FROM medical_rag.identity_roles r "
                "WHERE r.tenant_id=l.tenant_id AND r.account_id=l.account_id), "
                "(SELECT b.patient_id FROM medical_rag.identity_bindings b "
                "WHERE b.tenant_id=l.tenant_id AND b.account_id=l.account_id AND b.verified) FROM live l",
                (self._tenant_id, digest, self._idle, require_csrf, csrf_digest),
        ).fetchone()
        if row is None:
            raise AuthenticationDenied()
        account, mfa, roles, patient = row
        try:
            roles = frozenset(Role(role) for role in roles)
        except ValueError:
            raise AuthenticationDenied() from None
        return Principal(str(account), str(self._tenant_id), roles, True, True,
                         str(patient) if patient else None, mfa)

    def revoke(self, token: str, *, csrf_token: str) -> None:
        """Revoke this session with CSRF proof without disclosing existence."""
        digest = _hash_token(token)
        csrf_digest = _hash_token(csrf_token)
        with self._transaction() as connection:
            connection.execute("UPDATE medical_rag.identity_sessions SET revoked=true "
                               "WHERE tenant_id=%s AND token_hash=%s AND csrf_hash=%s",
                               (self._tenant_id, digest, csrf_digest))
