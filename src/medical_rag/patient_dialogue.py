"""Authorized PostgreSQL dialogue-state adapter for the patient entry.

Every function here assumes the caller is already inside an authorised
``PatientService._operation`` transaction: identity, consent and conversation
ownership have been checked with fresh statements, and the connection carries the
RLS scope. Nothing in this module may create an identity, open a transaction, or
widen what the runtime role can read.

Reads use a short ``FOR SHARE`` lock so the window and the state come from the same
row version. Writes compare the content version and never accept a model-supplied
revision.
"""

from __future__ import annotations

import json
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from .consent_policy import PatientIdentity
from .dialogue_models import (
    ConversationState,
    DialogueRun,
    DialogueSnapshot,
    RecentTurn,
    Topic,
)
from .dialogue_state import advance_state
from .question_resolver import validate_resolution_sources
from .short_term import WINDOW_TURNS, recent_window

# One window query, in ordinal order: only an assistant turn whose immediate
# predecessor is a user turn counts as a completed question/answer pair. Taking six
# raw messages instead would miss a pair after several unanswered user messages.
WINDOW_SQL = """
WITH ordered AS (
    SELECT turn_id,role,content,answer_type,ordinal,
           lag(role) OVER (ORDER BY ordinal) AS previous_role,
           lag(turn_id) OVER (ORDER BY ordinal) AS previous_turn_id,
           lag(content) OVER (ORDER BY ordinal) AS previous_content
    FROM medical_rag.conversation_turns
    WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s
)
SELECT previous_turn_id,previous_content,content,answer_type,ordinal
FROM ordered
WHERE role='assistant' AND previous_role='user'
ORDER BY ordinal DESC LIMIT %s
"""


class ConversationContextChanged(Exception):
    """Static stale-context conflict; never carries conversation content."""


def _scope(subject: PatientIdentity, conversation_id: UUID) -> tuple[object, ...]:
    return (subject.tenant_id, subject.patient_id, conversation_id)


def _state(value: object) -> ConversationState | None:
    """Decode a stored jsonb value through the strict JSON path."""
    if value is None:
        return None
    try:
        encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return ConversationState.model_validate_json(encoded)
    except ValueError:
        return None


def read_snapshot(db: Connection, subject: PatientIdentity,
                  conversation_id: UUID) -> DialogueSnapshot:
    """Read the bounded window, the state and the content version together.

    Args:
        db: The caller's already-authorised transaction.
        subject: The freshly verified patient identity.
        conversation_id: A conversation already proven to belong to the subject.

    Returns:
        A snapshot whose window never includes an unanswered user turn. A state
        whose sources can no longer be re-proved degrades to ``None`` while the
        authorised window is still returned.
    """
    row = db.execute(
        "SELECT dialogue_state,context_version FROM medical_rag.conversations "
        "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s FOR SHARE",
        _scope(subject, conversation_id),
    ).fetchone()
    if row is None:
        return DialogueSnapshot()
    state = _state(row[0])
    if state is not None and not _state_sourced(db, subject, conversation_id, state):
        state = None
    rows = db.execute(WINDOW_SQL, (*_scope(subject, conversation_id), WINDOW_TURNS)).fetchall()
    kinds = {str(item[0]): item[3] for item in rows}
    candidates = [{"turn": str(item[0]), "question": item[1], "answer": item[2]}
                  for item in rows]
    window = recent_window(candidates, WINDOW_TURNS)
    recent = tuple(
        RecentTurn(turn_id=item["turn"], user=item["user"], assistant=item["assistant"],
                   answer_type=kinds.get(item["turn"], "answer"))
        for item in window)
    # The patient entry keeps the digest empty on purpose: the local SQLite digest
    # describes a different store and must not be borrowed for patient history.
    return DialogueSnapshot(recent=recent, summary="", state=state,
                            context_version=int(row[1] or 0))


def _state_sourced(db: Connection, subject: PatientIdentity, conversation_id: UUID,
                   state: ConversationState) -> bool:
    """Re-prove each persisted topic against its authoritative turn."""
    topics: list[Topic] = []
    if state.active_topic is not None:
        topics.append(state.active_topic)
    if state.pending_clarification is not None:
        topics.extend(state.pending_clarification.candidates)
    for topic in topics:
        try:
            turn_id = UUID(topic.source_turn_id)
        except (TypeError, ValueError):
            return False
        row = db.execute(
            "SELECT role,content FROM medical_rag.conversation_turns "
            "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s AND turn_id=%s",
            (*_scope(subject, conversation_id), turn_id),
        ).fetchone()
        if row is None or row[0] != topic.source_role or topic.name not in row[1]:
            return False
    return True


def commit_state(db: Connection, subject: PatientIdentity, conversation_id: UUID,
                 run: DialogueRun, *, turn_id: str) -> None:
    """Commit the reduced state under a content-version check.

    Args:
        db: The caller's final transaction, after the messages were inserted.
        subject: The re-verified patient identity.
        conversation_id: The locked conversation.
        run: The request-scoped slot with a validated resolution.
        turn_id: The user turn UUID this turn is authoritative for.

    Raises:
        ConversationContextChanged: When another write landed since the snapshot.
        ValueError: When the resolution or state cannot be validated.
    """
    resolution = run.resolution
    if resolution is None:
        raise ValueError("No resolution to commit")
    validate_resolution_sources(resolution, resolution.original_question, run.snapshot)
    state = advance_state(run.snapshot.state, resolution, turn_id=turn_id)
    row = db.execute(
        "UPDATE medical_rag.conversations "
        "SET dialogue_state=%s, dialogue_revision=%s, context_version=context_version+1 "
        "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s AND context_version=%s "
        "RETURNING context_version",
        (Jsonb(json.loads(state.model_dump_json())), state.revision,
         *_scope(subject, conversation_id), run.snapshot.context_version),
    ).fetchone()
    if row is None:
        # Rolling the transaction back is the caller's job: the message inserts and
        # the stale state must disappear together.
        raise ConversationContextChanged()
    run.snapshot = DialogueSnapshot(
        state=state, recent=run.snapshot.recent, summary=run.snapshot.summary,
        context_version=int(row[0]))


def bump_context(db: Connection, subject: PatientIdentity, conversation_id: UUID) -> None:
    """Advance the content version for a write that establishes no semantics.

    A lone appended user turn, an answer stored while the switch was off, or a
    technical failure all change the conversation without redefining its topic.
    """
    locked = db.execute(
        "SELECT conversation_id FROM medical_rag.conversations "
        "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s FOR UPDATE",
        _scope(subject, conversation_id),
    ).fetchone()
    if locked is None:
        raise ConversationContextChanged()
    db.execute(
        "UPDATE medical_rag.conversations SET context_version=context_version+1 "
        "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s",
        _scope(subject, conversation_id),
    )


def invalidate_context(db: Connection, subject: PatientIdentity,
                       conversation_id: UUID) -> None:
    """Drop the derived state for a turn whose semantics were never computed.

    Used when the dialogue switch is off: the answer still changes the
    conversation, so the version moves and the state is marked for rebuild.
    """
    locked = db.execute(
        "SELECT conversation_id FROM medical_rag.conversations "
        "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s FOR UPDATE",
        _scope(subject, conversation_id),
    ).fetchone()
    if locked is None:
        raise ConversationContextChanged()
    db.execute(
        "UPDATE medical_rag.conversations "
        "SET dialogue_state=NULL, context_version=context_version+1 "
        "WHERE tenant_id=%s AND patient_id=%s AND conversation_id=%s",
        _scope(subject, conversation_id),
    )


__all__ = [
    "ConversationContextChanged",
    "bump_context",
    "commit_state",
    "invalidate_context",
    "read_snapshot",
]
