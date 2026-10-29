"""Durable extraction tasks are bounded, idempotent and independent of chat success."""

import json
import sqlite3
import time
from contextlib import closing
from threading import Event

import pytest
from test_memory import candidates, enqueue, episode_candidates


@pytest.fixture
def store(tmp_path):
    from medical_rag.memory import MemoryStore
    return MemoryStore(tmp_path / "db.sqlite3", tmp_path / "episodes")


class Extractor:
    def __init__(self, reply=None, error=None):
        self.reply = reply or episode_candidates()
        self.error = error
        self.calls = []

    def complete_with_timeout(self, provider, system, user, timeout_seconds):
        self.calls.append((provider, system, json.loads(user), timeout_seconds))
        if self.error:
            raise self.error
        return json.dumps(self.reply, ensure_ascii=False)


def worker(store, transport):
    from medical_rag.memory_worker import MemoryWorker
    return MemoryWorker(store, transport)


def test_worker_extracts_and_new_instance_recalls(store):
    enqueue(store)
    transport = Extractor()
    assert worker(store, transport).process_once() is True
    assert store.job_status("run1")["status"] == "saved"
    reopened = type(store)(store.path, store.root)
    assert reopened.state()["profiles"] == []
    assert "讨论了随访安排" in reopened.snapshot("随访")[1]
    assert len(reopened.state()["episodes"]) == 1
    assert worker(reopened, transport).process_once() is False
    assert len(transport.calls) == 1
    assert transport.calls[0][3] == 8
    assert transport.calls[0][2]["question"] == "我喜欢简短中文回答"
    assert "顶层只能包含 episode" in transport.calls[0][1]
    assert "profiles" not in transport.calls[0][1]


def test_unconsented_sensitive_episode_is_redacted_once_and_saved_clean(store):
    enqueue(store, question="我有哮喘", consent=False)
    calls = []

    class Redactor(Extractor):
        def complete_with_timeout(self, provider, system, user, timeout_seconds):
            calls.append(system)
            if len(calls) == 1:
                return json.dumps({"episode": {**episode_candidates()["episode"],
                                               "sensitive": True}}, ensure_ascii=False)
            return json.dumps({"episode": {"title": "随访讨论", "summary": "讨论了随访安排",
                "events": [], "decisions": [], "unresolved": [], "followups": [],
                "tags": ["随访"], "sensitive": False}}, ensure_ascii=False)

    assert worker(store, Redactor()).process_once() is True
    assert len(calls) == 2
    assert "脱敏" in calls[1]
    assert store.job_status("run1")["status"] == "saved"
    assert len(store.state()["episodes"]) == 1
    assert not store.state()["episodes"][0]["sensitive"]


def test_redaction_failure_cancels_storage_instead_of_saving_sensitive(store):
    enqueue(store, question="我有哮喘", consent=False)
    runner = worker(store, Extractor(reply={
        "episode": {**episode_candidates()["episode"], "sensitive": True}}))
    assert runner.process_once() is True
    assert store.job_status("run1")["status"] == "skipped"
    assert store.state()["episodes"] == []


def test_redacted_episode_with_residual_sensitive_text_is_cancelled_at_write_gate(store):
    enqueue(store, question="我喜欢简短中文回答", consent=False)
    raw = {"episode": {"title": "随访讨论", "summary": "我每日服用metformin，已确诊哮喘",
        "events": [], "decisions": [], "unresolved": [], "followups": [],
        "tags": [], "sensitive": False}}
    store.complete_episode(store.claim(), raw, redacted=True)
    assert store.state()["episodes"] == []
    assert store.job_status("run1")["status"] == "skipped"


def test_unconsented_non_sensitive_extraction_keeps_single_model_call(store):
    enqueue(store, question="请介绍哮喘的一般知识", consent=False)
    transport = Extractor(reply={"episode": {**episode_candidates()["episode"],
                                             "sensitive": False}})
    assert worker(store, transport).process_once() is True
    assert len(transport.calls) == 1
    assert store.job_status("run1")["status"] == "saved"


def test_transient_failure_uses_finite_delayed_retries(store):
    enqueue(store)
    transport = Extractor(error=TimeoutError())
    runner = worker(store, transport)
    for attempt, delay in [(1, 1), (2, 5), (3, 5)]:
        before = time.time()
        assert runner.process_once() is True
        with closing(sqlite3.connect(store.path)) as connection, connection:
            row = connection.execute("SELECT attempts,available,status FROM memory_jobs").fetchone()
            assert row[0] == attempt
            assert row[1] >= before + delay
            if attempt < 3:
                assert row[2] == "pending"
                assert runner.process_once() is False
                connection.execute("UPDATE memory_jobs SET available=0")
            else:
                assert row[2] == "failed"
    assert len(transport.calls) == 3
    assert store.state()["profiles"] == []


def test_invalid_extraction_does_not_retry_or_save_partial_data(store):
    enqueue(store)
    transport = Extractor(reply={"episode": "malformed"})
    runner = worker(store, transport)
    assert runner.process_once()
    assert store.job_status("run1")["status"] == "failed"
    assert runner.process_once() is False
    assert store.state()["profiles"] == store.state()["episodes"] == []


def test_new_contract_profile_response_is_terminally_rejected(store):
    enqueue(store)
    runner = worker(store, Extractor(reply=candidates()))
    assert runner.process_once()
    assert store.job_status("run1")["status"] == "failed"
    assert runner.process_once() is False
    assert store.state()["profiles"] == store.state()["episodes"] == []


def test_legacy_contract_response_saves_episode_without_touching_manual_profile(store):
    store.save_profile("response_style", "人工偏好", "preferences")
    enqueue(store)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE memory_jobs SET contract_version=1")
    assert worker(store, Extractor(reply=candidates())).process_once()
    assert store.job_status("run1")["status"] == "saved"
    assert len(store.state()["episodes"]) == 1
    assert store.state()["profiles"][0]["value"] == "人工偏好"
    assert store.state()["profiles"][0]["source"] == "manual"


def test_expired_lease_recovers_with_new_token_and_stale_result_cannot_write(store):
    enqueue(store)
    old = store.claim()
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE memory_jobs SET lease_until=0")
    new = store.claim()
    assert new["token"] != old["token"]
    store.complete(old, episode_candidates())
    assert store.state()["profiles"] == []
    store.complete(new, episode_candidates())
    assert store.job_status("run1")["status"] == "saved"


def test_worker_lifecycle_stops_without_waiting_for_new_jobs(store):
    runner = worker(store, Extractor())
    runner.start()
    runner.start()  # one worker per instance
    runner.stop()
    assert not runner.running


def test_timeout_discards_late_reply_without_spawning_more_model_calls(store, monkeypatch):
    import medical_rag.memory_worker as module

    enqueue(store)
    released = Event()
    finished = Event()

    class Slow(Extractor):
        def complete_with_timeout(self, *args):
            released.wait(2)
            finished.set()
            return json.dumps(episode_candidates())

    monkeypatch.setattr(module, "TIMEOUT_SECONDS", 0.05)
    runner = worker(store, Slow())
    try:
        assert runner.process_once()
        assert store.job_status("run1")["status"] == "pending"
        assert runner.process_once() is False  # the old adapter still owns the slot
    finally:
        released.set()
        assert finished.wait(1)
    assert store.state()["profiles"] == []  # no callback can save the late response


def test_terminal_write_failure_cleans_pending_index_and_temp(store, monkeypatch):
    enqueue(store)
    original_write = store._write

    def crash(relative, content):
        original_write(relative, content)
        raise OSError("crashed after replace")

    monkeypatch.setattr(store, "_write", crash)
    runner = worker(store, Extractor())
    for _ in range(3):
        assert runner.process_once()
        with closing(sqlite3.connect(store.path)) as connection, connection:
            connection.execute("UPDATE memory_jobs SET available=0")
    assert store.job_status("run1")["status"] == "failed"
    assert list(store.root.rglob("*.md")) == []
    assert store.state()["episodes"] == []


def test_background_worker_recovers_registered_pending_task_after_new_instance(store):
    enqueue(store)
    saved = Event()

    class Notifying(type(store)):
        def complete(self, *args):
            super().complete(*args)
            saved.set()

    restarted = Notifying(store.path, store.root)
    runner = worker(restarted, Extractor())
    runner.start()
    try:
        assert saved.wait(2)
    finally:
        runner.stop()
    assert store.job_status("run1")["status"] == "saved"
