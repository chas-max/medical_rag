"""Disposable-PostgreSQL tests for the authorised dialogue-state adapter."""
# ruff: noqa: F401,F811 -- imported pytest fixtures are used by injection

import json
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_oidc_login import database as oidc_database
from test_oidc_login import signing_key
from test_patient_vertical import (
    APP_ORIGIN,
    TENANT,
    auth_system,
    database,
    grant,
    logged_in,
    provision,
    system,
)

from medical_rag.consent_policy import PatientIdentity, SubjectAge
from medical_rag.dialogue_models import DialogueRun, Resolution
from medical_rag.patient_dialogue import (
    ConversationContextChanged,
    bump_context,
    commit_state,
    invalidate_context,
    read_snapshot,
)

PATIENT = "00000000-0000-0000-0000-000000000301"


@pytest.mark.integration
def test_dialogue_columns_exist_and_foreign_scope_is_hidden(system, database):
    client, _, _, admin = system
    headers = logged_in(system)
    grant(client, headers)
    identifier = client.post("/conversations", headers=headers,
                             json={"title": "合成会话"}).json()["data"]["id"]
    runtime, _ = database
    with psycopg.connect(**admin) as owner:
        assert owner.execute(
            "SELECT dialogue_revision,context_version FROM medical_rag.conversations "
            "WHERE conversation_id=%s", (identifier,),
        ).fetchone() == (0, 0)
    with psycopg.connect(**runtime) as db:
        assert db.execute(
            "SELECT dialogue_state FROM medical_rag.conversations WHERE conversation_id=%s",
            (identifier,),
        ).fetchall() == []


@pytest.fixture
def scoped(database):
    """One owner-created conversation with a completed synthetic turn."""
    runtime, admin = database
    conversation = uuid4()
    asked = uuid4()
    with psycopg.connect(**admin) as db:
        db.execute("INSERT INTO medical_rag.conversations"
                   "(tenant_id,patient_id,conversation_id,title) VALUES (%s,%s,%s,%s)",
                   (TENANT, PATIENT, conversation, "合成会话"))
        db.execute("INSERT INTO medical_rag.conversation_turns"
                   "(tenant_id,patient_id,conversation_id,turn_id,content,role) "
                   "VALUES (%s,%s,%s,%s,%s,'user')",
                   (TENANT, PATIENT, conversation, asked, "糖尿病呢？"))
        db.execute("INSERT INTO medical_rag.conversation_turns"
                   "(tenant_id,patient_id,conversation_id,turn_id,content,role,citations) "
                   "VALUES (%s,%s,%s,%s,%s,'assistant',%s)",
                   (TENANT, PATIENT, conversation, uuid4(), "合成回答", Jsonb([])))
    return runtime, admin, conversation, asked


def _identity() -> PatientIdentity:
    return PatientIdentity(TENANT, PATIENT, None, SubjectAge.ADULT, True)


def _scope(db, tenant=TENANT, patient=PATIENT) -> None:
    db.execute("SELECT set_config('app.patient_scope',"
               "pg_current_xact_id()::text||'/'||%s||'/'||%s,true)", (tenant, patient))


def _resolution(source: str) -> Resolution:
    return Resolution.model_validate_json(json.dumps({
        "original_question": "糖尿病呢？", "status": "resolved", "relation": "follow_up",
        "resolved_question": "糖尿病在日常生活中需要注意什么？",
        "topic": {"name": "糖尿病", "type": "disease", "source_turn_id": source,
                  "source_role": "user"},
        "intent": "daily_precautions", "intent_text": "日常注意事项",
        "references": [], "clarification": None,
    }, ensure_ascii=False))


@pytest.mark.integration
def test_runtime_role_reads_and_commits_the_state(scoped):
    runtime, admin, conversation, asked = scoped
    with psycopg.connect(**runtime) as db:
        _scope(db)
        snapshot = read_snapshot(db, _identity(), conversation)
        assert snapshot.context_version == 0
        assert [turn.turn_id for turn in snapshot.recent] == [str(asked)]
        run = DialogueRun(snapshot, _resolution(str(asked)))
        commit_state(db, _identity(), conversation, run, turn_id=str(asked))
        db.commit()
    with psycopg.connect(**admin) as owner:
        state, revision, version = owner.execute(
            "SELECT dialogue_state,dialogue_revision,context_version "
            "FROM medical_rag.conversations WHERE conversation_id=%s",
            (conversation,)).fetchone()
        assert state["active_topic"]["name"] == "糖尿病"
        assert revision == 1
        assert version == 1


@pytest.mark.integration
def test_two_commits_with_the_same_version_only_accept_the_first(scoped):
    runtime, admin, conversation, asked = scoped
    with psycopg.connect(**runtime) as db:
        _scope(db)
        snapshot = read_snapshot(db, _identity(), conversation)
        db.commit()
    first = DialogueRun(snapshot, _resolution(str(asked)))
    second = DialogueRun(snapshot, _resolution(str(asked)))
    with psycopg.connect(**runtime) as db:
        _scope(db)
        commit_state(db, _identity(), conversation, first, turn_id=str(asked))
        db.commit()
    with psycopg.connect(**runtime) as db:
        _scope(db)
        with pytest.raises(ConversationContextChanged):
            commit_state(db, _identity(), conversation, second, turn_id=str(asked))
        db.rollback()
    with psycopg.connect(**admin) as owner:
        assert owner.execute(
            "SELECT dialogue_revision FROM medical_rag.conversations "
            "WHERE conversation_id=%s", (conversation,)).fetchone() == (1,)


@pytest.mark.integration
def test_appending_only_advances_the_content_version(scoped):
    runtime, admin, conversation, _ = scoped
    with psycopg.connect(**runtime) as db:
        _scope(db)
        bump_context(db, _identity(), conversation)
        db.commit()
    with psycopg.connect(**admin) as owner:
        assert owner.execute(
            "SELECT dialogue_state,dialogue_revision,context_version "
            "FROM medical_rag.conversations WHERE conversation_id=%s",
            (conversation,)).fetchone() == (None, 0, 1)


@pytest.mark.integration
def test_identity_columns_cannot_be_updated_by_the_runtime_role(scoped):
    runtime, _admin, conversation, _ = scoped
    with psycopg.connect(**runtime) as db:
        _scope(db)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("UPDATE medical_rag.conversations SET title='改名' "
                       "WHERE conversation_id=%s", (conversation,))
        db.rollback()


@pytest.mark.integration
def test_state_is_hidden_from_another_patient_scope(scoped):
    runtime, _admin, conversation, _ = scoped
    with psycopg.connect(**runtime) as db:
        _scope(db, patient="00000000-0000-0000-0000-000000000302")
        assert read_snapshot(db, PatientIdentity(
            TENANT, "00000000-0000-0000-0000-000000000302", None, SubjectAge.ADULT, True),
            conversation).state is None


@pytest.mark.integration
def test_read_snapshot_decodes_a_committed_state(scoped):
    runtime, _admin, conversation, asked = scoped
    with psycopg.connect(**runtime) as db:
        _scope(db)
        snapshot = read_snapshot(db, _identity(), conversation)
        commit_state(db, _identity(), conversation, DialogueRun(
            snapshot, _resolution(str(asked))), turn_id=str(asked))
        db.commit()
    with psycopg.connect(**runtime) as db:
        _scope(db)
        reloaded = read_snapshot(db, _identity(), conversation)
        assert reloaded.state.active_topic.name == "糖尿病"
        assert reloaded.context_version == 1
        assert reloaded.recent[-1].user == "糖尿病呢？"


@pytest.mark.integration
@pytest.mark.parametrize("stored", [
    {"schema_version": 1, "revision": 1, "active_topic": {
        "name": "伪造对象", "type": "disease", "source_turn_id": "00000000-0000-0000-0000-0000000009ff",
        "source_role": "user"}},
    {"broken": True},
])
def test_an_unverifiable_state_is_dropped_but_the_window_survives(scoped, stored):
    runtime, admin, conversation, _ = scoped
    with psycopg.connect(**admin) as db:
        db.execute("UPDATE medical_rag.conversations SET dialogue_state=%s "
                   "WHERE conversation_id=%s", (Jsonb(stored), conversation))
    with psycopg.connect(**runtime) as db:
        _scope(db)
        snapshot = read_snapshot(db, _identity(), conversation)
        assert snapshot.state is None
        assert [turn.user for turn in snapshot.recent] == ["糖尿病呢？"]


@pytest.mark.integration
def test_commit_without_a_resolution_is_rejected(scoped):
    runtime, _admin, conversation, asked = scoped
    with psycopg.connect(**runtime) as db:
        _scope(db)
        snapshot = read_snapshot(db, _identity(), conversation)
        with pytest.raises(ValueError):
            commit_state(db, _identity(), conversation, DialogueRun(snapshot),
                         turn_id=str(asked))
        db.rollback()


@pytest.mark.integration
def test_bump_on_a_foreign_scope_is_rejected(scoped):
    runtime, _admin, conversation, _ = scoped
    with psycopg.connect(**runtime) as db:
        _scope(db, patient="00000000-0000-0000-0000-000000000302")
        foreign = PatientIdentity(TENANT, "00000000-0000-0000-0000-000000000302",
                                  None, SubjectAge.ADULT, True)
        with pytest.raises(ConversationContextChanged):
            bump_context(db, foreign, conversation)
        db.rollback()


@pytest.mark.integration
def test_invalidate_keeps_the_version_and_drops_the_state(scoped):
    runtime, admin, conversation, asked = scoped
    with psycopg.connect(**runtime) as db:
        _scope(db)
        snapshot = read_snapshot(db, _identity(), conversation)
        commit_state(db, _identity(), conversation, DialogueRun(
            snapshot, _resolution(str(asked))), turn_id=str(asked))
        db.commit()
    with psycopg.connect(**runtime) as db:
        _scope(db)
        invalidate_context(db, _identity(), conversation)
        db.commit()
    with psycopg.connect(**admin) as owner:
        assert owner.execute(
            "SELECT dialogue_state,dialogue_revision,context_version "
            "FROM medical_rag.conversations WHERE conversation_id=%s",
            (conversation,)).fetchone() == (None, 1, 2)
