"""Confirmation erasure, authorization, atomicity and bounded retention."""

import pytest

from medical_rag.memory import MemoryStore
from medical_rag.profile_models import Mutation, ProfilePlan
from medical_rag.profile_store import ProfileStore
from medical_rag.types import ApiError


@pytest.fixture
def store(tmp_path):
    return ProfileStore(MemoryStore(tmp_path / "chat.sqlite3", tmp_path / "episodes"))


def pending(store, mutations, consent=False):
    snapshot = store.snapshot()
    return store.apply("original", "用户原文", "c1", "run1", 1,
        ProfilePlan(snapshot.profile_revision, snapshot.privacy_revision,
                    confirmations=tuple(mutations)), consent)


def test_sensitive_confirmation_requires_both_original_and_current_authorization(store):
    mutation = Mutation("health", "set", "高血压", True, "explicit", "我患有高血压")
    with pytest.raises(ApiError):
        pending(store, [mutation])
    outcome = pending(store, [mutation], True)
    assert outcome.confirmation_requires_consent
    with pytest.raises(ApiError):
        store.confirm(outcome.confirmation_token, "yes", "c1", True, False)
    assert store.snapshot().profiles == {}
    assert store.confirm(outcome.confirmation_token, "yes-consent", "c1", True, True).actions[0].status == "added"


def test_sensitive_deletion_confirmation_needs_no_save_consent(store):
    store.memory.save_profile("health", "高血压", "health", True)
    outcome = pending(store, [Mutation("health", "delete", "", True, "explicit", "忘记健康画像")])
    assert not outcome.confirmation_requires_consent
    store.confirm(outcome.confirmation_token, "yes", "c1", True, False)
    assert store.snapshot().profiles == {}


def test_confirm_batch_rolls_back_profile_and_token_on_disk_failure(store, monkeypatch):
    mutations = [Mutation(key, "set", value, False, "explicit", "本人陈述") for key, value in
                 [("occupation", "护士"), ("language", "中文")]]
    outcome = pending(store, mutations)
    original = store.memory._upsert_profile

    def fail_second(connection, key, *args):
        if key == "language":
            raise OSError("disk unavailable")
        original(connection, key, *args)

    monkeypatch.setattr(store.memory, "_upsert_profile", fail_second)
    with pytest.raises(ApiError):
        store.confirm(outcome.confirmation_token, "yes", "c1", True, False)
    assert store.snapshot().profiles == {}
    monkeypatch.setattr(store.memory, "_upsert_profile", original)
    store.confirm(outcome.confirmation_token, "yes", "c1", True, False)
    assert set(store.snapshot().profiles) == {"occupation", "language"}


def test_expired_mutation_bodies_are_scrubbed_even_without_more_chat(store):
    pending(store, [Mutation("occupation", "set", "护士", False, "explicit", "我是护士")])
    with store.memory._connect(True) as connection:
        connection.execute("UPDATE profile_confirmations SET expires_at=0")
    assert store.memory.claim() is None
    with store.memory._connect() as connection:
        row = connection.execute("SELECT status,mutations FROM profile_confirmations").fetchone()
    assert row["status"] == "expired" and row["mutations"] == "[]"


def test_full_profile_forgetting_keeps_episode_and_chat_history(tmp_path, monkeypatch):
    from test_memory import enqueue, episode_candidates
    from test_memory_api import memory_client
    from test_profile_chat_api import StaticAnalyzer, payload

    from medical_rag.profile_models import Candidate, IntentDecision

    analyzer = StaticAnalyzer(IntentDecision(needs_answer=False, candidates=[Candidate(
        key="*", operation="forget_all", support="忘记我的全部画像", subject="user",
        modality="positive", sensitivity="normal")]))
    api, app, _ = memory_client(tmp_path, monkeypatch, analyzer=analyzer)
    memory = app.state.memory
    memory.save_profile("occupation", "护士", "background")
    memory.save_profile("health", "高血压", "health", True)
    enqueue(memory)
    memory.complete_episode(memory.claim(), episode_candidates())
    before = memory.state()["episodes"]
    assert len(before) == 1
    result = api.post("/chat/runs", json=payload("忘记我的全部画像")).json()["data"]
    assert len(memory.state()["profiles"]) == 2
    confirmed = api.post("/chat/confirmations", json={"request_id": "yes", "conversation_id": "c1",
        "token": result["profile"]["confirmation_token"], "accept": True, "memory_consent": False})
    assert confirmed.status_code == 200, confirmed.text
    assert memory.state()["profiles"] == []
    assert memory.state()["episodes"] == before
    assert api.get("/conversations/c1").json()["data"]["messages"][0]["content"] == "忘记我的全部画像"
