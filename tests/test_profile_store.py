"""Real SQLite profile transactions, privacy controls and confirmation binding."""

import sqlite3
from contextlib import closing

import pytest

from medical_rag.memory import MemoryStore
from medical_rag.profile_models import Mutation, ProfilePlan
from medical_rag.types import ApiError


@pytest.fixture
def profiles(tmp_path):
    from medical_rag.profile_store import ProfileStore
    return ProfileStore(MemoryStore(tmp_path / "chat.sqlite3", tmp_path / "episodes"))


def plan_for(store, value="护士", operation="set", key="occupation", confirm=False):
    snapshot = store.snapshot()
    mutation = Mutation(key, operation, value, key == "health", "explicit", "我是一名" + value)
    return ProfilePlan(snapshot.profile_revision, snapshot.privacy_revision,
                       confirmations=(mutation,) if confirm else (),
                       mutations=() if confirm else (mutation,))


def apply(store, request="r1", plan=None, question="我是一名护士", order=1, consent=False):
    return store.apply(request, question, "c1", "run-" + request, order,
                       plan or plan_for(store), consent)


def test_persistence_versions_and_idempotency(profiles):
    before = profiles.snapshot()
    plan = plan_for(profiles)
    assert apply(profiles, plan=plan).actions[0].status == "added"
    assert apply(profiles, plan=plan).actions[0].status == "added"
    current = profiles.snapshot()
    assert current.profile_revision == before.profile_revision + 1
    assert current.privacy_revision == before.privacy_revision
    assert current.profiles["occupation"]["value"] == "护士"
    assert type(profiles)(MemoryStore(profiles.memory.path, profiles.memory.root)).snapshot() == current
    with pytest.raises(ApiError, match="标识"):
        apply(profiles, question="different")


def test_manual_edit_and_stale_plans_cannot_override(profiles):
    old = plan_for(profiles)
    profiles.memory.save_profile("occupation", "教师", "background")
    outcome = apply(profiles, plan=old)
    assert outcome.status == "conflict"
    assert profiles.snapshot().profiles["occupation"]["value"] == "教师"


def test_delete_then_replay_or_older_message_cannot_resurrect(profiles):
    original = plan_for(profiles)
    apply(profiles, plan=original, order=2)
    delete = plan_for(profiles, "", "delete")
    assert apply(profiles, "r2", delete, "忘掉我的职业", 3).actions[0].status == "deleted"
    apply(profiles, plan=original, order=2)
    assert profiles.snapshot().profiles == {}
    assert apply(profiles, "old", order=1).status == "conflict"


def test_sensitive_save_requires_consent_but_delete_does_not(profiles):
    plan = plan_for(profiles, "高血压", key="health")
    with pytest.raises(ApiError):
        apply(profiles, plan=plan)
    assert profiles.snapshot().profiles == {}
    apply(profiles, plan=plan, consent=True)
    deleted = apply(profiles, "r2", plan_for(profiles, "", "delete", "health"), order=2)
    assert deleted.actions[0].status == "deleted"


def test_confirmation_is_bound_consumable_and_cancellable(profiles):
    profiles.memory.save_profile("occupation", "教师", "background")
    result = apply(profiles, plan=plan_for(profiles, confirm=True))
    assert result.confirmation_token
    assert profiles.snapshot().profiles["occupation"]["value"] == "教师"
    with pytest.raises(ApiError):
        profiles.confirm(result.confirmation_token, "confirm", "other", True, False)
    accepted = profiles.confirm(result.confirmation_token, "confirm", "c1", True, False)
    assert accepted.actions[0].status == "updated"
    assert profiles.confirm(result.confirmation_token, "confirm", "c1", True, False) == accepted
    assert profiles.snapshot().profiles["occupation"]["value"] == "护士"
    with closing(sqlite3.connect(profiles.memory.path)) as connection:
        assert connection.execute("SELECT mutations FROM profile_confirmations").fetchone()[0] == "[]"


@pytest.mark.parametrize("change", ["version", "expired", "cancel"])
def test_confirmation_changes_nothing_when_invalid(profiles, change):
    result = apply(profiles, plan=plan_for(profiles, confirm=True))
    if change == "version":
        profiles.memory.save_profile("occupation", "教师", "background")
    elif change == "expired":
        with closing(sqlite3.connect(profiles.memory.path)) as connection, connection:
            connection.execute("UPDATE profile_confirmations SET expires_at=0")
    before = profiles.snapshot()
    if change == "cancel":
        profiles.confirm(result.confirmation_token, "confirm", "c1", False, False)
    else:
        with pytest.raises(ApiError):
            profiles.confirm(result.confirmation_token, "confirm", "c1", True, False)
    assert profiles.snapshot() == before


def test_transaction_failure_does_not_partially_write(profiles, monkeypatch):
    snapshot = profiles.snapshot()
    mutations = (Mutation("occupation", "set", "护士", False, "auto", "我是护士"),
                 Mutation("language", "set", "中文", False, "auto", "请用中文"))
    original = profiles.memory._upsert_profile
    def broken(connection, key, *args):
        if key == "language":
            raise OSError("disk")
        original(connection, key, *args)
    monkeypatch.setattr(profiles.memory, "_upsert_profile", broken)
    with pytest.raises(ApiError):
        apply(profiles, plan=ProfilePlan(snapshot.profile_revision, snapshot.privacy_revision, mutations))
    assert profiles.snapshot().profiles == {}


def test_exact_sensitive_fact_subtraction_does_not_need_save_consent(profiles):
    profiles.memory.save_profile("health", "高血压；哮喘", "health", True)
    snapshot = profiles.snapshot()
    subtract = Mutation("health", "set", "哮喘", True, "explicit", "我不再有高血压")
    result = apply(profiles, plan=ProfilePlan(snapshot.profile_revision, snapshot.privacy_revision,
                                            mutations=(subtract,)), question="我不再有高血压")
    assert result.actions[0].status == "updated"
    assert profiles.snapshot().profiles["health"]["value"] == "哮喘"
    arbitrary = plan_for(profiles, "糖尿病", key="health")
    with pytest.raises(ApiError):
        apply(profiles, request="r2", plan=arbitrary, question="我患有糖尿病", order=2)


def test_disabled_snapshot_does_not_expose_values_to_analyzer(profiles):
    profiles.memory.save_profile("occupation", "护士", "background")
    profiles.memory.set_enabled(False)
    assert profiles.snapshot().profiles == {}
