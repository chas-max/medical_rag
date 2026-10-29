"""Bounded intent candidates and server-owned profile operation contracts."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProfileKey = Literal["display_name", "occupation", "language", "response_style",
                     "interests", "goals", "health"]
ActionStatus = Literal["added", "updated", "deleted", "unchanged", "blocked",
                       "needs_confirmation", "conflict", "unsupported", "failed"]


class Candidate(BaseModel):
    """An untrusted model proposal, never an executable database instruction."""

    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(min_length=1, max_length=128)
    operation: Literal["assert", "replace", "retract", "forget", "forget_all"]
    value: str = Field(default="", max_length=500)
    support: str = Field(min_length=1, max_length=500)
    subject: Literal["user", "other", "hypothetical", "quoted", "unknown"]
    modality: Literal["positive", "negative", "question", "uncertain"]
    sensitivity: Literal["normal", "sensitive", "unknown"]


class IntentDecision(BaseModel):
    """Composable memory operations, memory lookup, and ordinary answering."""

    model_config = ConfigDict(extra="forbid", strict=True)
    needs_answer: bool
    lookup_keys: list[ProfileKey] = Field(default_factory=list, max_length=7)
    candidates: list[Candidate] = Field(default_factory=list, max_length=12)
    clarification: str | None = Field(default=None, max_length=500)


class ProfileAction(BaseModel):
    """Safe public feedback without duplicating remembered facts."""

    key: str
    status: ActionStatus
    message: str


class ProfileOutcome(BaseModel):
    """Actual transaction outcome, distinct from episode job status."""

    status: Literal["processed", "skipped", "disabled", "failed", "conflict"]
    actions: list[ProfileAction] = Field(default_factory=list)
    confirmation_token: str | None = None
    confirmation_expires_at: float | None = None
    confirmation_requires_consent: bool = False


@dataclass(frozen=True)
class ProfileSnapshot:
    """One immutable version boundary for planning."""

    enabled: bool
    profile_revision: int
    privacy_revision: int
    profiles: dict[str, dict]


@dataclass(frozen=True)
class Mutation:
    """A validated local mutation; never parsed directly from HTTP."""

    key: str
    operation: Literal["set", "delete"]
    value: str
    sensitive: bool
    source: Literal["auto", "explicit"]
    support: str


@dataclass(frozen=True)
class ProfilePlan:
    """Only permitted writes and separately protected confirmation candidates."""

    profile_revision: int
    privacy_revision: int
    mutations: tuple[Mutation, ...] = ()
    confirmations: tuple[Mutation, ...] = ()
    actions: tuple[ProfileAction, ...] = ()
