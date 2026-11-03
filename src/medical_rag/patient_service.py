"""Live, fictional-data patient operations, with primary-DB authority per call.

Patient shared locks serialize content against exclusive consent changes.
After lock waits, fresh statements recheck patient identity, account/session
authority, then latest purpose-specific consent. Already authorized operations
holding shared locks are in flight: withdrawal waits, not stream cancellation.
This is not institutional identity proofing, clinical or legal acceptance.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from psycopg import Connection

from .access_policy import Action, Resource, is_allowed
from .consent_policy import (
    ConsentPurpose,
    ConsentRecord,
    PatientIdentity,
    SubjectAge,
    has_current_consent,
)
from .identity_sessions import AuthorizedTransaction, SessionStore
from .security_audit import AuditAction, AuditStore

JsonObject = dict[str, object]
ContentRow = tuple[UUID, str, datetime]


class PatientDenied(Exception):
    """No current verified adult self-patient authority/consent."""


class ResourceMissing(Exception):
    """Missing, malformed or invisible owned resource."""


@dataclass(frozen=True, slots=True)
class HealthNotice:
    """Institution-supplied immutable actual notice; no pre-approved default."""

    version: str
    text: str
    validity_seconds: int

    def __post_init__(self) -> None:
        if (type(self.version) is not str or not self.version.strip()
                or self.version != self.version.strip() or len(self.version) > 128
                or type(self.text) is not str or not self.text.strip() or len(self.text) > 64000
                or type(self.validity_seconds) is not int or not 1 <= self.validity_seconds <= 31536000):
            raise ValueError("Trusted health notice configuration required")


def _identifier(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ResourceMissing() from None


def _text(value: str, maximum: int) -> None:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError("Invalid request")


def _page(limit: int, offset: int) -> None:
    if (type(limit) is not int or not 1 <= limit <= 100
            or type(offset) is not int or not 0 <= offset <= 10000):
        raise ValueError("Invalid pagination")


def _item(row: ContentRow, field: str) -> JsonObject:
    return {"id": str(row[0]), field: row[1], "created_at": row[2].isoformat()}


class PatientService:
    """No disconnected repository/snapshot principal may authorize these calls."""

    def __init__(self, sessions: SessionStore, notice: HealthNotice) -> None:
        if not isinstance(sessions, SessionStore) or type(notice) is not HealthNotice:
            raise ValueError("Trusted session store and health notice required")
        self.sessions = sessions
        self.notice = notice
        self.audit = AuditStore(sessions)

    def _subject(self, authorized: AuthorizedTransaction, *, exclusive: bool = False,
                 action: Action = Action.PATIENT_READ) -> PatientIdentity:
        principal = authorized.principal
        resource = Resource(principal.tenant_id, principal.patient_id)
        if not is_allowed(principal, action, resource):
            raise PatientDenied()
        db = authorized.connection
        scope = (principal.tenant_id, principal.patient_id)
        db.execute("SELECT set_config('app.patient_scope',pg_current_xact_id()::text||'/'||%s||'/'||%s,true)", scope)
        row = db.execute("SELECT patient_id FROM medical_rag.patient_subjects "
                         "WHERE tenant_id=%s AND patient_id=%s " + ("FOR UPDATE" if exclusive else "FOR SHARE"),
                         scope).fetchone()
        if row is None:
            raise PatientDenied()
        # Fresh statement AFTER the lock: don't rely on the waiting SELECT's
        # snapshot, including for trusted provisioning changes to verification.
        age, verified = db.execute("SELECT age_classification,identity_verified FROM medical_rag.patient_subjects "
                                   "WHERE tenant_id=%s AND patient_id=%s", scope).fetchone()
        principal = authorized.refresh()
        if age != SubjectAge.ADULT.value or verified is not True:
            raise PatientDenied()
        return PatientIdentity(principal.tenant_id, principal.patient_id, principal.user_id,
                               SubjectAge(age), verified)

    def _consent(self, db: Connection, subject: PatientIdentity,
                 action: Action = Action.PATIENT_READ) -> tuple[bool, str | None]:
        # Never filter version/granted before selecting the latest purpose event.
        row = db.execute("SELECT grantor_account_id,notice_version,granted,recorded_at,expires_at "
                         "FROM medical_rag.patient_consent_events WHERE tenant_id=%s AND patient_id=%s "
                         "AND purpose='health_information' ORDER BY event_order DESC LIMIT 1",
                         (subject.tenant_id, subject.patient_id)).fetchone()
        now = db.execute("SELECT clock_timestamp()").fetchone()[0]
        if row is None:
            return False, None
        grantor, version, granted, recorded, expires = row
        record = ConsentRecord(subject.tenant_id, subject.patient_id, ConsentPurpose.HEALTH_INFORMATION,
                               version, str(grantor), granted, not granted, recorded, expires, recorded)
        current = has_current_consent(subject, record, ConsentPurpose.HEALTH_INFORMATION, action,
                                      required_version=self.notice.version, now=now)
        return current, expires.isoformat() if current else None

    @contextmanager
    def _operation(self, token: str, *, audit_action: AuditAction,
                   csrf_token: str | None = None, mutation: bool = False,
                   consent: bool = True, exclusive: bool = False
                   ) -> Iterator[tuple[Connection, PatientIdentity, bool, str | None]]:
        with self.audit.operation(token, audit_action, mutation=mutation, csrf_token=csrf_token) as authorized:
            subject = self._subject(authorized, exclusive=exclusive,
                                    action=Action.PATIENT_WRITE if mutation else Action.PATIENT_READ)
            current, expires = self._consent(authorized.connection, subject,
                                            Action.PATIENT_WRITE if mutation else Action.PATIENT_READ)
            if consent and not current:
                raise PatientDenied()
            yield authorized.connection, subject, current, expires

    def me(self, token: str) -> JsonObject:
        with self.audit.operation(token, AuditAction.IDENTITY_READ) as authorized:
            principal = authorized.principal
            eligible, current = False, False
            try:
                subject = self._subject(authorized)
            except PatientDenied:
                pass
            else:
                eligible = True
                current, _ = self._consent(authorized.connection, subject)
            return {"user_id": principal.user_id, "tenant_id": principal.tenant_id,
                    "roles": sorted(principal.roles), "binding": {"verified": principal.patient_id is not None},
                    "permissions": {"identity": True, "health_consent": eligible, "conversations": current,
                                    "support_self": True,
                                    "support_queue": is_allowed(principal, Action.SUPPORT_CASE_MANAGE,
                                                                Resource(principal.tenant_id)),
                                    "audit": is_allowed(principal, Action.AUDIT_READ, Resource(principal.tenant_id))}}

    def _status(self, current: bool, expires: str | None) -> JsonObject:
        return {"notice": {"version": self.notice.version, "text": self.notice.text,
                           "validity_seconds": self.notice.validity_seconds},
                "granted": current, "expires_at": expires}

    def consent_status(self, token: str) -> JsonObject:
        with self._operation(token, audit_action=AuditAction.CONSENT_READ, consent=False) as (_, _, current, expires):
            return self._status(current, expires)

    def change_consent(self, token: str, *, csrf_token: str, granted: bool,
                       version: str | None = None) -> JsonObject:
        if type(granted) is not bool or (granted and version != self.notice.version):
            raise ValueError("Invalid consent")
        action = AuditAction.CONSENT_GRANT if granted else AuditAction.CONSENT_WITHDRAW
        with self._operation(token, audit_action=action, csrf_token=csrf_token,
                             mutation=True, consent=False, exclusive=True) as (db, subject, _, _):
            db.execute("INSERT INTO medical_rag.patient_consent_events "
                       "(event_id,tenant_id,patient_id,grantor_account_id,purpose,notice_version,granted,expires_at) "
                       "VALUES (%s,%s,%s,%s,'health_information',%s,%s,"
                       "clock_timestamp()+(%s*interval '1 second'))",
                       (uuid4(), subject.tenant_id, subject.patient_id, subject.account_id,
                        self.notice.version, granted, self.notice.validity_seconds if granted else 0))
            current, expires = self._consent(db, subject)
            return self._status(current, expires)

    def create_conversation(self, token: str, *, csrf_token: str, title: str) -> JsonObject:
        _text(title, 200)
        with self._operation(token, audit_action=AuditAction.CONVERSATION_CREATE,
                             csrf_token=csrf_token, mutation=True) as (db, subject, _, _):
            row = db.execute("INSERT INTO medical_rag.conversations(tenant_id,patient_id,conversation_id,title) "
                             "VALUES (%s,%s,%s,%s) RETURNING conversation_id,title,created_at",
                             (subject.tenant_id, subject.patient_id, uuid4(), title)).fetchone()
            return _item(row, "title")

    def list_conversations(self, token: str, *, limit: int = 20, offset: int = 0) -> JsonObject:
        _page(limit, offset)
        with self._operation(token, audit_action=AuditAction.CONVERSATION_LIST) as (db, subject, _, _):
            rows = db.execute("SELECT conversation_id,title,created_at FROM medical_rag.conversations "
                              "WHERE tenant_id=%s AND patient_id=%s ORDER BY created_at DESC,conversation_id "
                              "LIMIT %s OFFSET %s", (subject.tenant_id, subject.patient_id, limit, offset)).fetchall()
            return {"items": [_item(row, "title") for row in rows], "limit": limit, "offset": offset}

    @staticmethod
    def _conversation(db: Connection, subject: PatientIdentity, identifier: UUID) -> ContentRow:
        row = db.execute("SELECT conversation_id,title,created_at FROM medical_rag.conversations "
                         "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s",
                         (subject.tenant_id, subject.patient_id, identifier)).fetchone()
        if row is None:
            raise ResourceMissing()
        return row

    def get_conversation(self, token: str, identifier: str) -> JsonObject:
        conversation_id = _identifier(identifier)
        with self._operation(token, audit_action=AuditAction.CONVERSATION_READ) as (db, subject, _, _):
            return _item(self._conversation(db, subject, conversation_id), "title")

    def append_turn(self, token: str, identifier: str, *, csrf_token: str, content: str) -> JsonObject:
        conversation_id = _identifier(identifier)
        _text(content, 16000)
        with self._operation(token, audit_action=AuditAction.TURN_APPEND,
                             csrf_token=csrf_token, mutation=True) as (db, subject, _, _):
            self._conversation(db, subject, conversation_id)
            row = db.execute("INSERT INTO medical_rag.conversation_turns "
                             "(tenant_id,patient_id,conversation_id,turn_id,content) VALUES (%s,%s,%s,%s,%s) "
                             "RETURNING turn_id,content,created_at",
                             (subject.tenant_id, subject.patient_id, conversation_id, uuid4(), content)).fetchone()
            return _item(row, "content")

    def list_turns(self, token: str, identifier: str, *, limit: int = 20, offset: int = 0) -> JsonObject:
        conversation_id = _identifier(identifier)
        _page(limit, offset)
        with self._operation(token, audit_action=AuditAction.TURN_LIST) as (db, subject, _, _):
            self._conversation(db, subject, conversation_id)
            rows = db.execute("SELECT turn_id,content,created_at FROM medical_rag.conversation_turns "
                              "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s "
                              "ORDER BY created_at,turn_id LIMIT %s OFFSET %s",
                              (subject.tenant_id, subject.patient_id, conversation_id, limit, offset)).fetchall()
            return {"items": [_item(row, "content") for row in rows], "limit": limit, "offset": offset}
