"""Pure conversation-state transitions plus SQLite write helpers.

The reducer is a pure function so both entries and every test can apply the same
rules without a database. The SQLite helpers only ever use the caller's open
connection: they never start their own transaction, so the answer, the state and
the content version commit or roll back together.
"""

from __future__ import annotations

import json
import sqlite3

from .dialogue_models import ConversationState, Resolution, Topic
from .types import ApiError

# Answered-but-unsatisfying outcomes that still mean "this topic is now the
# conversation's subject". Technical truncation or parse failures are excluded so a
# broken answer cannot silently move the conversation to a new disease.
SEMANTIC_ABSTAIN_REASONS = frozenset(
    {"ABSTAIN_NO_EVIDENCE", "ABSTAIN_LOW_SCORE", "ABSTAIN_UNANCHORED"}
)

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_states (
    conversation_id TEXT PRIMARY KEY,
    state_json TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    context_version INTEGER NOT NULL DEFAULT 0 CHECK (context_version >= 0),
    through_turn_id TEXT NOT NULL DEFAULT ''
);
"""


def remap(topic: Topic | None, turn_id: str) -> Topic | None:
    """Replace the ``current`` placeholder with the authoritative turn id.

    Only ``current`` is rewritten: an older source must keep pointing at the turn
    that actually said it.

    Args:
        topic: Topic whose source may be the ``current`` placeholder.
        turn_id: Authoritative id of the turn being committed.

    Returns:
        A topic with a concrete source id, or ``None`` unchanged.
    """
    if topic is None or topic.source_turn_id != "current":
        return topic
    return topic.model_copy(update={"source_turn_id": turn_id})


def advance_state(
    previous: ConversationState | None, resolution: Resolution, *, turn_id: str
) -> ConversationState:
    """Apply one resolution to the previous state and assign the new revision.

    Args:
        previous: The state read in the same snapshot, or ``None`` on a first turn.
        resolution: The already validated resolution for this turn.
        turn_id: Authoritative id of the answered turn.

    Returns:
        A new immutable state. Its ``revision`` is one higher than the input's.

    Raises:
        ValueError: If the derived state would exceed the serialized budget.
    """
    state = previous or ConversationState()
    common = {"revision": state.revision + 1, "through_turn_id": turn_id}
    if resolution.status == "clarification":
        pending = resolution.clarification
        if pending is not None:
            pending = pending.model_copy(
                update={
                    "source_turn_id": turn_id,
                    "candidates": tuple(remap(topic, turn_id) for topic in pending.candidates),
                }
            )
        elif state.pending_clarification is not None:
            # A resolver failure may only ask for detail; the design's failure row
            # says "only maintain the commit boundary", so an earlier real pending
            # item survives instead of being erased by a degraded turn.
            pending = state.pending_clarification
        values = {**state.model_dump(), **common, "pending_clarification": pending}
    else:
        full = resolution.resolved_question or ""
        values = {
            "schema_version": 1,
            **common,
            "active_topic": remap(resolution.topic, turn_id),
            "active_intent": resolution.intent,
            "active_intent_text": resolution.intent_text,
            # A very long standalone question is kept verbatim in the answer store;
            # repeating it here would spend the 8 KiB budget on a copy.
            "last_resolved_question": full if len(full) <= 1000 else "",
            "pending_clarification": None,
        }
    return ConversationState.model_validate_json(
        json.dumps(values, ensure_ascii=False, default=lambda value: value.model_dump())
    )


def can_commit_semantics(status: str, output: dict[str, object]) -> bool:
    """Whether a finished run may advance the structured conversation state.

    Args:
        status: The workflow run status.
        output: The public workflow output mapping.

    Returns:
        ``True`` for a real answer, a meaningful abstention or a clarification, and
        ``False`` for every technical failure mode.
    """
    if status != "success":
        return False
    if output.get("answer_type") == "clarification":
        return True
    if not output.get("abstained"):
        return bool(str(output.get("answer", "")).strip())
    reason = output.get("abstain_reason")
    if reason is None:
        # Legacy history stored the same enum under a camelCase key.
        reason = output.get("abstainReason")
    return reason in SEMANTIC_ABSTAIN_REASONS


def decode_state(raw: object) -> ConversationState | None:
    """Decode a stored state, treating any invalid payload as absent."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return ConversationState.model_validate_json(raw)
    except ValueError:
        return None


def write_sqlite_state(connection: sqlite3.Connection, conversation_id: str,
                       state: ConversationState, expected_version: int) -> None:
    """Commit one state under a content-version check inside the caller's transaction.

    Args:
        connection: The final ``BEGIN IMMEDIATE`` connection of this turn.
        conversation_id: Conversation id the state belongs to.
        state: The reduced state to persist.
        expected_version: The ``context_version`` the snapshot was read at.

    Raises:
        ApiError: ``CONVERSATION_CONTEXT_CHANGED`` when another write landed first.
    """
    payload = state.model_dump_json()
    changed = connection.execute(
        "UPDATE conversation_states SET state_json=?, revision=?, "
        "context_version=context_version+1, through_turn_id=? "
        "WHERE conversation_id=? AND context_version=?",
        (payload, state.revision, state.through_turn_id, conversation_id, expected_version),
    ).rowcount
    if changed:
        return
    if expected_version == 0:
        try:
            connection.execute(
                "INSERT INTO conversation_states"
                "(conversation_id,state_json,revision,context_version,through_turn_id) "
                "VALUES (?,?,?,?,?)",
                (conversation_id, payload, state.revision, 1, state.through_turn_id),
            )
            return
        except sqlite3.IntegrityError:
            pass
    raise ApiError("CONVERSATION_CONTEXT_CHANGED", "会话已更新，请重新发送问题", 409)


def bump_sqlite_context(connection: sqlite3.Connection, conversation_id: str) -> None:
    """Advance the content version once for a write that establishes no semantics.

    ``state_json`` is left untouched: an append or an imported turn changes what the
    conversation contains, not what it is about.
    """
    connection.execute(
        "INSERT INTO conversation_states(conversation_id, context_version) VALUES (?, 1) "
        "ON CONFLICT(conversation_id) DO UPDATE SET context_version = context_version + 1",
        (conversation_id,),
    )


def invalidate_sqlite_state(connection: sqlite3.Connection, conversation_id: str) -> None:
    """Mark a state as needing a rebuild while keeping its version monotonic.

    A turn answered while the feature switch was off has semantics nobody
    observed; on re-enable the derived state must be rebuilt from the authorised
    window instead of silently continuing from the old boundary.
    """
    connection.execute(
        "INSERT INTO conversation_states(conversation_id, state_json, context_version) "
        "VALUES (?, NULL, 1) ON CONFLICT(conversation_id) DO UPDATE SET "
        "state_json = NULL, context_version = context_version + 1",
        (conversation_id,),
    )


__all__ = [
    "SEMANTIC_ABSTAIN_REASONS",
    "STATE_SCHEMA",
    "advance_state",
    "bump_sqlite_context",
    "can_commit_semantics",
    "decode_state",
    "invalidate_sqlite_state",
    "remap",
    "write_sqlite_state",
]
