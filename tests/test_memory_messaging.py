"""Memory caller transaction, targeted dispatch and privacy fences survive AMQP."""

import sqlite3
from contextlib import closing

from test_memory import enqueue
from test_memory_worker import Extractor

from medical_rag.memory import MemoryStore
from medical_rag.memory_worker import MemoryWorker


def test_enqueue_outbox_is_in_caller_transaction_and_targeted(tmp_path):
    store = MemoryStore(tmp_path / "history.db", tmp_path / "episodes")
    enqueue(store, "one")
    enqueue(store, "two", order=2)
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT count(*) FROM task_outbox").fetchone()[0] == 2
        db.execute("BEGIN")
        store.enqueue(
            db, "rolledback", "chat", 3, "question", {"answer": "answer"}, 0, False
        )
        db.rollback()
        assert db.execute("SELECT count(*) FROM task_outbox").fetchone()[0] == 2
    worker = MemoryWorker(store, Extractor())
    assert worker.process("two", 0)
    assert store.job_status("one")["status"] == "pending"
    assert store.job_status("two")["status"] == "saved"
    assert not worker.process("two", 0)
    store.clear()
    assert not worker.process("one", 0)


def test_retry_dispatch_generation_and_terminal_dlq_atomic(tmp_path):
    store = MemoryStore(tmp_path / "history.db", tmp_path / "episodes")
    enqueue(store, "one")
    worker = MemoryWorker(store, Extractor(error=TimeoutError()))
    assert worker.process("one", 0)
    assert not worker.process("one", 0)
    with store._connect(True) as db:
        row = db.execute("SELECT * FROM memory_jobs").fetchone()
        assert row["dispatch_seq"] == 1 and row["attempts"] == 1
        db.execute("UPDATE memory_jobs SET available=0")
    worker.transport = Extractor(error=ValueError("malformed"))
    assert worker.process("one", 1)
    with store._connect() as db:
        assert (
            db.execute(
                'SELECT count(*) FROM task_outbox WHERE destination="dead"'
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT question FROM memory_jobs").fetchone()[0] == ""


def test_backfill_valid_legacy_jobs_without_resetting_attempts(tmp_path):
    store = MemoryStore(tmp_path / "history.db", tmp_path / "episodes")
    enqueue(store, "one")
    enqueue(store, "two", order=2)
    with store._connect(True) as db:
        db.execute("DELETE FROM task_outbox")
        db.execute("UPDATE memory_jobs SET attempts=2 WHERE run_id='one'")
        db.execute("UPDATE memory_jobs SET status='cancelled' WHERE run_id='two'")
    with store._connect() as db:
        assert db.execute("SELECT task_id FROM task_outbox").fetchall()[0][0] == "one"
        assert (
            db.execute(
                "SELECT attempts FROM memory_jobs WHERE run_id='one'"
            ).fetchone()[0]
            == 2
        )


def test_worker_refreshes_transport_for_each_task(tmp_path):
    store = MemoryStore(tmp_path / "history.db", tmp_path / "episodes")
    enqueue(store, "one")
    enqueue(store, "two", order=2)
    transports = []

    def factory():
        transports.append(Extractor())
        return transports[-1]

    worker = MemoryWorker(store, transport_factory=factory)
    assert worker.process("one", 0)
    assert worker.process("two", 0)
    assert len(transports) == 2


def test_targeted_delivery_while_old_model_thread_lives_is_durably_deferred(tmp_path):
    from types import SimpleNamespace

    store = MemoryStore(tmp_path / "history.db", tmp_path / "episodes")
    enqueue(store, "one")
    worker = MemoryWorker(store, Extractor())
    worker._model_thread = SimpleNamespace(is_alive=lambda: True)
    assert worker.process("one", 0) is False
    with store._connect() as db:
        job = db.execute("SELECT attempts,dispatch_seq FROM memory_jobs").fetchone()
        assert tuple(job) == (0, 1)
        assert (
            db.execute(
                "SELECT count(*) FROM task_outbox WHERE dispatch_seq=1"
            ).fetchone()[0]
            == 1
        )
    worker._model_thread = None
    with store._connect(True) as db:
        db.execute("UPDATE memory_jobs SET available=0")
    assert not worker.process("one", 0)
    assert worker.process("one", 1)
