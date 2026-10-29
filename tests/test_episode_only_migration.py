"""Background tasks can create episodes, never modify profile facts."""

import sqlite3
from contextlib import closing

import pytest
from test_memory import candidates, enqueue

from medical_rag.memory import MemoryStore

LEGACY_JOBS_SCHEMA = """
CREATE TABLE memory_jobs (
    run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
    source_order INTEGER NOT NULL, question TEXT NOT NULL, output TEXT NOT NULL,
    revision INTEGER NOT NULL, consent INTEGER NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, available REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0, token TEXT, error TEXT,
    created_at TEXT NOT NULL);
"""


def test_real_legacy_schema_revokes_processing_token_once(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(LEGACY_JOBS_SCHEMA)
        for run, status, revision in [("active", "processing", 0), ("cancelled", "cancelled", 0),
                                      ("stale", "processing", 4)]:
            connection.execute(
                "INSERT INTO memory_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run, "local-user", "chat", 1, "我喜欢简短中文回答", '{}', revision,
                 1, status, 1, 0, 99999999999, "old-token", None, "2026-10-18"))
    store = MemoryStore(path, tmp_path / "episodes")
    assert store.state()["profiles"] == []
    with closing(sqlite3.connect(path)) as connection:
        rows = {row[0]: row[1:] for row in connection.execute(
            "SELECT run_id,status,token,contract_version FROM memory_jobs")}
    assert rows["active"] == ("pending", None, 1)
    assert rows["cancelled"][0] == rows["stale"][0] == "cancelled"
    job = store.claim()
    assert job["run_id"] == "active" and job["token"] != "old-token"
    late = {**job, "token": "old-token"}
    store.complete(late, candidates())
    assert store.state()["episodes"] == []
    # A second initialization cannot revoke a current worker's new lease.
    MemoryStore(path, store.root).state()
    store.complete_episode(job, {"episode": candidates()["episode"]})
    assert store.job_status("active")["status"] == "saved"
    assert store.state()["profiles"] == []


def test_legacy_profiles_are_ignored_even_when_malformed(tmp_path):
    store = MemoryStore(tmp_path / "chat.sqlite3", tmp_path / "episodes")
    enqueue(store)
    job = store.claim()
    job["contract_version"] = 1
    store.complete_episode(job, {"profiles": "not a profile list", "episode": candidates()["episode"]})
    assert store.state()["profiles"] == []
    assert store.job_status("run1")["status"] == "saved"


def test_legacy_exhausted_lease_cannot_become_stuck_pending(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(LEGACY_JOBS_SCHEMA)
        connection.execute(
            "INSERT INTO memory_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("run1", "local-user", "chat", 1, "私人原文", '{}', 0, 1,
             "processing", 3, 0, 99999999999, "old-token", None, "2026-10-18"))
    store = MemoryStore(path, tmp_path / "episodes")
    assert store.claim() is None
    assert store.job_status("run1")["status"] == "failed"
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT question,output,token FROM memory_jobs").fetchone() == ("", "{}", None)


def test_legacy_reply_never_writes_profiles(tmp_path):
    store = MemoryStore(tmp_path / "chat.sqlite3", tmp_path / "episodes")
    enqueue(store)
    job = store.claim()
    job["contract_version"] = 1
    store.complete(job, candidates())
    assert store.state()["profiles"] == []
    assert len(store.state()["episodes"]) == 1


def test_new_contract_rejects_profile_payload(tmp_path):
    store = MemoryStore(tmp_path / "chat.sqlite3", tmp_path / "episodes")
    enqueue(store)
    job = store.claim()
    assert job["contract_version"] == 2
    with pytest.raises(ValueError):
        store.complete_episode(job, candidates())
    assert store.state()["profiles"] == []


def test_episode_only_contract_persists_after_restart(tmp_path):
    store = MemoryStore(tmp_path / "chat.sqlite3", tmp_path / "episodes")
    enqueue(store)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE memory_jobs SET contract_version=1")
    reopened = MemoryStore(store.path, store.root)
    job = reopened.claim()
    reopened.complete_episode(job, {"episode": candidates()["episode"]})
    assert reopened.state()["profiles"] == []
    assert reopened.job_status("run1")["status"] == "saved"
