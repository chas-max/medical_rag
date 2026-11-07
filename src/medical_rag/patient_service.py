"""Live, fictional-data patient operations, with primary-DB authority per call.

Patient shared locks serialize content against exclusive consent changes.
After lock waits, fresh statements recheck patient identity, account/session
authority, then latest purpose-specific consent. Already authorized operations
holding shared locks are in flight: withdrawal waits, not stream cancellation.
This is not institutional identity proofing, clinical or legal acceptance.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from .access_policy import Action, Resource, is_allowed
from .consent_policy import (
    ConsentPurpose,
    ConsentRecord,
    PatientIdentity,
    SubjectAge,
    has_current_consent,
)
from .dialogue_models import DialogueRun, DialogueSnapshot, Resolution
from .dialogue_state import can_commit_semantics
from .identity_sessions import AuthorizedTransaction, SessionStore
from .patient_chat import ChatUnavailable
from .patient_dialogue import (
    ConversationContextChanged,
    bump_context,
    commit_state,
    invalidate_context,
    read_snapshot,
)
from .security_audit import AuditAction, AuditStore

JsonObject = dict[str, object]
ContentRow = tuple[UUID, str, datetime]


class ChatAnswerer(Protocol):
    """Trusted local answer source; failures are static and carry no text."""

    def answer(self, question: str, *,
               dialogue: DialogueSnapshot | None = None) -> dict[str, object]: ...


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


TurnRow = tuple[UUID, str, str, object, str, datetime]


def _turn(row: TurnRow) -> JsonObject:
    turn_id, content, role, citations, answer_type, created = row
    if not isinstance(citations, list):
        citations = []
    return {
        "id": str(turn_id),
        "content": content,
        "role": role,
        "citations": citations,
        # Only the answer kind is exposed; internal dialogue state never leaves
        # the backend.
        "answer_type": answer_type,
        "created_at": created.isoformat(),
    }


def _title(question: str) -> str:
    return " ".join(question.split())[:30] or "新对话"


def _citation(value: object) -> JsonObject | None:
    """Keep only the bounded citation fields the patient UI can render.

    The answerer contract is trusted, but a faulty implementation must not be
    able to poison stored rows with shapes that make the whole turn page
    unreadable.
    """
    if type(value) is not dict:
        return None
    chunk_id, filename = value.get("chunk_id"), value.get("filename")
    if not isinstance(chunk_id, str) or not 0 < len(chunk_id) <= 256:
        return None
    if not isinstance(filename, str) or not 0 < len(filename) <= 512:
        return None
    section = value.get("section")
    citation: JsonObject = {
        "chunk_id": chunk_id,
        "filename": filename,
        "section": section if isinstance(section, str) and len(section) <= 512 else "",
    }
    page = value.get("page")
    if type(page) is int and 0 <= page <= 100000:
        citation["page"] = page
    origin = value.get("origin")
    if isinstance(origin, str) and 0 < len(origin) <= 32:
        citation["origin"] = origin
    excerpt = value.get("excerpt")
    if isinstance(excerpt, str) and 0 < len(excerpt) <= 2000:
        citation["excerpt"] = excerpt
    return citation


class PatientService:
    """No disconnected repository/snapshot principal may authorize these calls."""

    def __init__(self, sessions: SessionStore, notice: HealthNotice,
                 chat: ChatAnswerer | None = None, *,
                 dialogue_state_enabled: bool = False) -> None:
        if not isinstance(sessions, SessionStore) or type(notice) is not HealthNotice:
            raise ValueError("Trusted session store and health notice required")
        if chat is not None and not callable(getattr(chat, "answer", None)):
            raise ValueError("Trusted chat answerer required")
        self.sessions = sessions
        self.notice = notice
        self.chat = chat
        # Injected from trusted process configuration only, never a request body.
        self.dialogue_state_enabled = dialogue_state_enabled
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
                                    "chat": self.chat is not None,
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

    @staticmethod
    def _locked_conversation(db: Connection, subject: PatientIdentity,
                             identifier: UUID) -> tuple[ContentRow, int]:
        """Lock the owned row and return its fresh content version.

        Every content write locks in the same order (identity/consent, then
        conversation), so an append and an answer cannot interleave versions.
        """
        row = db.execute(
            "SELECT conversation_id,title,created_at,context_version "
            "FROM medical_rag.conversations "
            "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s FOR UPDATE",
            (subject.tenant_id, subject.patient_id, identifier)).fetchone()
        if row is None:
            raise ResourceMissing()
        return tuple(row[:3]), int(row[3])

    def get_conversation(self, token: str, identifier: str) -> JsonObject:
        conversation_id = _identifier(identifier)
        with self._operation(token, audit_action=AuditAction.CONVERSATION_READ) as (db, subject, _, _):
            return _item(self._conversation(db, subject, conversation_id), "title")

    def append_turn(self, token: str, identifier: str, *, csrf_token: str, content: str) -> JsonObject:
        conversation_id = _identifier(identifier)
        _text(content, 16000)
        with self._operation(token, audit_action=AuditAction.TURN_APPEND,
                             csrf_token=csrf_token, mutation=True) as (db, subject, _, _):
            self._locked_conversation(db, subject, conversation_id)
            row = db.execute("INSERT INTO medical_rag.conversation_turns "
                             "(tenant_id,patient_id,conversation_id,turn_id,content,role) "
                             "VALUES (%s,%s,%s,%s,%s,'user') "
                             "RETURNING turn_id,content,role,citations,answer_type,created_at",
                             (subject.tenant_id, subject.patient_id, conversation_id, uuid4(), content)).fetchone()
            # A lone append is a content change, not a completed turn: the version
            # moves so an in-flight answer is refused instead of landing stale.
            bump_context(db, subject, conversation_id)
            return _turn(row)

    def ask(self, token: str, *, csrf_token: str, conversation_id: str | None,
            question: str) -> JsonObject:
        """Precheck, answer outside any lock, then persist under fresh consent.

        A short write-permission transaction authorizes and verifies conversation
        ownership before the question leaves the process. The model call must not
        hold the patient row lock, so a concurrent withdrawal can complete while
        the answer is generated. Persistence then re-authorizes in one short
        transaction: if consent, role, binding, the session or the conversation's
        content version changed meanwhile, the generated answer is discarded and
        no turn or state is stored.
        """
        if self.chat is None:
            raise ChatUnavailable()
        _text(question, 16000)
        target = None if conversation_id is None else _identifier(conversation_id)
        snapshot = DialogueSnapshot()
        # Authorize and check ownership BEFORE any question leaves the process:
        # an unconsented or foreign request must not reach the model at all.
        with self._operation(token, audit_action=AuditAction.CHAT_ASK,
                             csrf_token=csrf_token, mutation=True) as (db, subject, _, _):
            if target is not None:
                self._conversation(db, subject, target)
                if self.dialogue_state_enabled:
                    snapshot = read_snapshot(db, subject, target)
        answer = (self.chat.answer(question, dialogue=snapshot)
                  if self.dialogue_state_enabled else self.chat.answer(question))
        if type(answer) is not dict:
            raise ChatUnavailable()
        content = answer.get("answer")
        if not isinstance(content, str) or not content.strip() or len(content) > 20000:
            raise ChatUnavailable()
        citations = answer.get("citations")
        safe_citations = []
        if type(citations) is list:
            for item in citations[:50]:
                citation = _citation(item)
                if citation is not None:
                    safe_citations.append(citation)
        citations = safe_citations
        abstained = answer.get("abstained") is True
        answer_type = answer.get("answer_type", "answer")
        if answer_type not in {"answer", "clarification"}:
            raise ChatUnavailable()
        with self._operation(token, audit_action=AuditAction.TURN_APPEND,
                             csrf_token=csrf_token, mutation=True) as (db, subject, _, _):
            if target is None:
                conversation = _item(
                    db.execute("INSERT INTO medical_rag.conversations"
                               "(tenant_id,patient_id,conversation_id,title) "
                               "VALUES (%s,%s,%s,%s) RETURNING conversation_id,title,created_at",
                               (subject.tenant_id, subject.patient_id, uuid4(), _title(question))).fetchone(),
                    "title")
                expected = 0
            else:
                locked, expected = self._locked_conversation(db, subject, target)
                conversation = _item(locked, "title")
                # The version fence only applies to the context-carrying path: with
                # the switch off there is no snapshot to be stale, and the counter
                # still advances so a later re-enable notices the gap.
                if self.dialogue_state_enabled and expected != snapshot.context_version:
                    raise ConversationContextChanged()
            target = UUID(str(conversation["id"]))
            asked = db.execute("INSERT INTO medical_rag.conversation_turns "
                               "(tenant_id,patient_id,conversation_id,turn_id,content,role) "
                               "VALUES (%s,%s,%s,%s,%s,'user') "
                               "RETURNING turn_id,content,role,citations,answer_type,created_at",
                               (subject.tenant_id, subject.patient_id, target, uuid4(), question)).fetchone()
            answered = db.execute("INSERT INTO medical_rag.conversation_turns "
                                  "(tenant_id,patient_id,conversation_id,turn_id,content,role,citations,"
                                  "answer_type) "
                                  "VALUES (%s,%s,%s,%s,%s,'assistant',%s,%s) "
                                  "RETURNING turn_id,content,role,citations,answer_type,created_at",
                                  (subject.tenant_id, subject.patient_id, target, uuid4(), content,
                                   Jsonb(citations), answer_type)).fetchone()
            self._commit_semantics(db, subject, target, snapshot, answer, expected,
                                   answer_type=answer_type, content=content,
                                   abstained=abstained, turn_id=asked[0])
            response: JsonObject = {"conversation": conversation, "question": _turn(asked),
                                    "answer": _turn(answered), "abstained": abstained,
                                    "answer_type": answer_type}
            if isinstance(answer.get("abstain_reason"), str):
                response["abstain_reason"] = answer["abstain_reason"][:64]
            return response

    def _commit_semantics(self, db: Connection, subject: PatientIdentity,
                          conversation_id: UUID, snapshot: DialogueSnapshot, answer: JsonObject,
                          expected: int, *, answer_type: str, content: str,
                          abstained: bool, turn_id: UUID) -> None:
        """Commit the state when this turn really established semantics.

        A clarification stores its candidates; a real answer or a meaningful
        abstention moves the topic; a technical failure, a cut-off answer or the
        switch being off only advances the content version.
        """
        resolution = answer.get("dialogue_resolution")
        output = {"answer": content, "abstained": abstained, "answer_type": answer_type,
                  "abstain_reason": answer.get("abstain_reason")}
        if not self.dialogue_state_enabled:
            # No one computed this turn's semantics: keep the content version
            # moving and mark the derived state for rebuild.
            invalidate_context(db, subject, conversation_id)
            return
        if resolution is not None and can_commit_semantics("success", output):
            try:
                validated = Resolution.model_validate_json(
                    json.dumps(resolution, ensure_ascii=False))
            except ValueError:
                raise ChatUnavailable() from None
            commit_state(db, subject, conversation_id,
                         DialogueRun(snapshot, validated), turn_id=str(turn_id))
            return
        bump_context(db, subject, conversation_id)

    def list_turns(self, token: str, identifier: str, *, limit: int = 20, offset: int = 0) -> JsonObject:
        conversation_id = _identifier(identifier)
        _page(limit, offset)
        with self._operation(token, audit_action=AuditAction.TURN_LIST) as (db, subject, _, _):
            self._conversation(db, subject, conversation_id)
            rows = db.execute("SELECT turn_id,content,role,citations,answer_type,created_at "
                              "FROM medical_rag.conversation_turns "
                              "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s "
                              "ORDER BY ordinal LIMIT %s OFFSET %s",
                              (subject.tenant_id, subject.patient_id, conversation_id, limit, offset)).fetchall()
            return {"items": [_turn(row) for row in rows], "limit": limit, "offset": offset}
