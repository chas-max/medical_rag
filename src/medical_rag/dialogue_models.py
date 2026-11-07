"""Strict models for structured dialogue state and per-request results.

Every model forbids extra fields, validates in strict mode and is frozen, so a
resolved question cannot be mutated by a downstream consumer. Containers are
tuples: a snapshot is a value, not a list someone can append to later.

The models are shared by the local chat entry, the patient entry, the workflow
``question.resolve`` node and the evaluation harness. They carry no storage
concern: persistence, versioning and commit boundaries belong to the callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Length budgets from the design document: names, source markers and the concrete
# intent phrase are individually bounded, and a whole state must fit in 8 KiB.
MAX_TOPIC_CHARS = 100
MAX_SOURCE_CHARS = 128
MAX_INTENT_TEXT_CHARS = 200
MAX_QUESTION_CHARS = 20000
MAX_STORED_QUESTION_CHARS = 1000
MAX_STATE_BYTES = 8192
MAX_REFERENCES = 6
MAX_CANDIDATES = 3
MAX_WINDOW_CHARS = 1200
MAX_SUMMARY_CHARS = 800
MAX_RECENT_TURNS = 3

TopicType = Literal["disease", "medication", "procedure", "lifestyle", "other"]

Intent = Literal[
    "daily_precautions",
    "etiology",
    "symptoms",
    "diagnosis",
    "treatment",
    "medication_usage",
    "prevention",
    "prognosis",
    "comparison",
    "other",
]

Relation = Literal["standalone", "topic_switch", "follow_up", "clarification_reply"]
ResolutionStatus = Literal["resolved", "clarification"]
SourceRole = Literal["user", "assistant"]


class StrictModel(BaseModel):
    """Base model: no extra fields, strict types, immutable instances."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Topic(StrictModel):
    """A discussion object with the source that authorises it.

    ``source_turn_id`` is ``current`` for the question being resolved, or a real
    turn identifier from the snapshot. ``source_role`` records whether the user or
    the assistant mentioned it; an assistant mention is context, never a fact
    about the user.
    """

    name: str = Field(min_length=1, max_length=MAX_TOPIC_CHARS)
    type: TopicType
    source_turn_id: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)
    source_role: SourceRole


class Reference(StrictModel):
    """One anaphor resolved to an entity plus the turn that backs it."""

    expression: str = Field(min_length=1, max_length=MAX_TOPIC_CHARS)
    entity: str = Field(min_length=1, max_length=MAX_TOPIC_CHARS)
    source_turn_id: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)


class PendingClarification(StrictModel):
    """At most one outstanding ambiguity, with bounded sourced candidates."""

    candidates: tuple[Topic, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    intent: Intent = "other"
    intent_text: str = Field(default="", max_length=MAX_INTENT_TEXT_CHARS)
    original_question: str = Field(default="", max_length=MAX_STORED_QUESTION_CHARS)
    source_turn_id: str = Field(default="current", min_length=1, max_length=MAX_SOURCE_CHARS)


class ResolverReply(StrictModel):
    """The model-facing resolution contract before the app assigns the original."""

    status: ResolutionStatus
    relation: Relation
    resolved_question: str | None = Field(
        default=None, min_length=1, max_length=MAX_QUESTION_CHARS
    )
    topic: Topic | None = None
    intent: Intent = "other"
    intent_text: str = Field(default="", max_length=MAX_INTENT_TEXT_CHARS)
    references: tuple[Reference, ...] = Field(default=(), max_length=MAX_REFERENCES)
    clarification: PendingClarification | None = None

    @model_validator(mode="after")
    def check_branch(self) -> ResolverReply:
        """A resolved reply must carry a question; a clarification must not."""
        if self.status == "resolved" and (
            not self.resolved_question or self.clarification is not None
        ):
            raise ValueError("Invalid resolved branch")
        if self.status == "clarification" and self.resolved_question is not None:
            raise ValueError("Clarification cannot execute retrieval")
        return self


class Resolution(ResolverReply):
    """A validated resolution; the original question is assigned by the app."""

    original_question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


class ConversationState(StrictModel):
    """The persisted semantic state of one conversation.

    ``revision`` counts semantic commits and is assigned by the application, never
    by the model. ``through_turn_id`` marks the last authoritative answered turn
    this state was derived from.
    """

    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    through_turn_id: str = Field(default="", max_length=MAX_SOURCE_CHARS)
    active_topic: Topic | None = None
    active_intent: Intent = "other"
    active_intent_text: str = Field(default="", max_length=MAX_INTENT_TEXT_CHARS)
    last_resolved_question: str = Field(default="", max_length=MAX_STORED_QUESTION_CHARS)
    pending_clarification: PendingClarification | None = None

    @model_validator(mode="after")
    def bounded_json(self) -> ConversationState:
        """Reject an oversized state instead of storing truncated invalid JSON."""
        if len(self.model_dump_json().encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("State budget exceeded")
        return self


class RecentTurn(StrictModel):
    """One finished turn inside the verbatim window."""

    turn_id: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)
    user: str = Field(min_length=1, max_length=200)
    assistant: str = Field(default="", max_length=400)
    answer_type: Literal["answer", "clarification"] = "answer"


class DialogueSnapshot(StrictModel):
    """One immutable, consistently read view of a conversation.

    The structured state is independent of the recent-window budget: the window is
    what a prompt may quote verbatim, while the state records the current topic and
    intent that survive beyond it.
    """

    state: ConversationState | None = None
    recent: tuple[RecentTurn, ...] = Field(default=(), max_length=MAX_RECENT_TURNS)
    summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    context_version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def bounded_window(self) -> DialogueSnapshot:
        """Keep the verbatim window inside its own character budget."""
        if sum(len(turn.user) + len(turn.assistant) for turn in self.recent) > MAX_WINDOW_CHARS:
            raise ValueError("Recent budget exceeded")
        return self

    def sources(self, question: str) -> dict[tuple[str, str], str]:
        """Map ``(turn_id, source_role)`` to the authoritative text behind it.

        ``current`` is always the question being resolved. Recent turns contribute
        both sides; persisted topics add their name to their own source so a
        follow-up can cite an object that already left the window.
        """
        sources: dict[tuple[str, str], str] = {("current", "user"): question}
        for turn in self.recent:
            sources[(turn.turn_id, "user")] = turn.user
            sources[(turn.turn_id, "assistant")] = turn.assistant
        if self.state is not None:
            for topic in _state_topics(self.state):
                key = (topic.source_turn_id, topic.source_role)
                sources[key] = sources.get(key, "") + "\n" + topic.name
        return sources


def _state_topics(state: ConversationState) -> list[Topic]:
    """Every topic a persisted state can authorise, active topic first."""
    topics: list[Topic] = []
    if state.active_topic is not None:
        topics.append(state.active_topic)
    if state.pending_clarification is not None:
        topics.extend(state.pending_clarification.candidates)
    return topics


@dataclass(slots=True)
class DialogueRun:
    """Request-scoped slot for the one resolution of the current question.

    It is deliberately not part of :class:`WorkflowRunResult`: keeping it in a
    caller-owned object stops the dataclass encoder from leaking internal state
    into traces, SSE frames or the public output.
    """

    snapshot: DialogueSnapshot
    resolution: Resolution | None = None


__all__ = [
    "ConversationState",
    "DialogueRun",
    "DialogueSnapshot",
    "Intent",
    "PendingClarification",
    "RecentTurn",
    "Reference",
    "Relation",
    "Resolution",
    "ResolutionStatus",
    "ResolverReply",
    "StrictModel",
    "Topic",
    "TopicType",
]
