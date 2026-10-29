"""Durable submission and idempotent document execution without heavy services."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from medical_rag.config import Settings
from medical_rag.types import ApiError


def store_at(tmp_path):
    from medical_rag.jobs import DocumentJobStore

    return DocumentJobStore(
        Settings(
            chat_history_file=str(tmp_path / "history.db"),
            document_uploads_dir=str(tmp_path / "uploads"),
        )
    )


def test_submission_pins_snapshot_and_atomic_file_without_broker(tmp_path):
    store = store_at(tmp_path)
    submitted = store.submit(
        "../../report.txt", b"public medical text", "public-source"
    )
    assert submitted["status"] == "queued"
    row = store.get(submitted["job_id"])
    assert row["stage"] == "queued" and row["attempts"] == 0
    assert "upload_path" not in json.dumps(row)
    with store.connect() as db:
        internal = dict(db.execute("SELECT * FROM document_jobs").fetchone())
        outbox = dict(db.execute("SELECT * FROM task_outbox").fetchone())
    assert Path(internal["upload_path"]).name == submitted["job_id"] + ".upload"
    assert json.loads(internal["snapshot"])["workflow"]["id"] == "document-ingestion"
    message = json.loads(outbox["payload"])
    assert set(message) == {
        "schema_version",
        "message_id",
        "task_type",
        "task_id",
        "dispatch_seq",
        "trace_id",
    }
    assert message["dispatch_seq"] == 0


@pytest.mark.parametrize(
    "filename,content,source",
    [
        ("x.txt", b"", None),
        ("x.exe", b"x", None),
        ("a" * 513 + ".txt", b"x", None),
        ("x.txt", b"x", "中" * 171),
    ],
)
def test_submission_validates_before_creating_files(
    tmp_path, filename, content, source
):
    store = store_at(tmp_path)
    with pytest.raises(ApiError):
        store.submit(filename, content, source)
    assert not list(store.upload_root.glob("*.upload"))


def test_processor_idempotence_missing_upload_and_safe_error(tmp_path):
    from medical_rag.jobs import DocumentProcessor

    store = store_at(tmp_path)
    first = store.submit("one.txt", b"text")
    calls = []
    service = SimpleNamespace(ingest=lambda *args: calls.append(args) or {"chunks": 2})
    processor = DocumentProcessor(store, service=service)
    assert processor.process(first["job_id"])
    assert not processor.process(first["job_id"])
    assert len(calls) == 1
    assert store.get(first["job_id"])["result"] == {"chunks": 2}
    assert not list(store.upload_root.glob("*.upload"))
    other = store.submit("two.txt", b"text")
    with store.connect() as db:
        path = db.execute(
            "SELECT upload_path FROM document_jobs WHERE job_id=?", (other["job_id"],)
        ).fetchone()[0]
    Path(path).unlink()
    assert processor.process(other["job_id"])
    assert store.get(other["job_id"])["status"] == "failed"
    assert str(tmp_path) not in json.dumps(store.get(other["job_id"]))


def test_same_source_ordering_survives_retry_and_stale_delivery(tmp_path):
    from medical_rag.jobs import DocumentProcessor

    store = store_at(tmp_path)
    one = store.submit("one.txt", b"text", "same")
    two = store.submit("two.txt", b"text", "same")

    def fail(*args):
        raise RuntimeError("secret /local/path")

    processor = DocumentProcessor(store, service=SimpleNamespace(ingest=fail))
    assert not processor.process(two["job_id"])
    assert processor.process(one["job_id"])
    assert store.get(one["job_id"])["status"] == "retrying"
    assert not processor.process(one["job_id"], 0)
    assert not processor.process(two["job_id"])
    with store.connect(True) as db:
        db.execute(
            "UPDATE document_jobs SET available_at=0 WHERE job_id=?", (one["job_id"],)
        )
    processor.service = SimpleNamespace(ingest=lambda *args: {"chunks": 1})
    assert processor.process(one["job_id"], 1)
    assert processor.process(two["job_id"])
    assert "secret" not in json.dumps(store.list_jobs())


def test_filesystem_failure_does_not_commit_job(tmp_path, monkeypatch):
    from medical_rag import jobs

    store = store_at(tmp_path)
    monkeypatch.setattr(
        jobs.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(ApiError):
        store.submit("a.txt", b"x")
    assert store.list_jobs() == []
    assert not list(store.upload_root.iterdir())


def test_cleanup_containment_and_health(tmp_path):
    store = store_at(tmp_path)
    submitted = store.submit("a.txt", b"x")
    orphan = store.upload_root / "orphan.upload"
    orphan.write_bytes(b"old")
    import os

    os.utime(orphan, (0, 0))
    outside = tmp_path / "outside.upload"
    outside.write_bytes(b"keep")
    assert store.cleanup_orphans() == 1
    assert outside.exists()
    assert len(list(store.upload_root.glob("*.upload"))) == 1
    store.heartbeat("relay", broker="connected")
    health = store.health()
    assert health["counts"]["queued"] == 1
    assert health["roles"]["relay"]["stale"] is False
    assert health["broker"] == "connected"
    assert store.list_jobs(active=True)[0]["job_id"] == submitted["job_id"]


def test_singleton_lock_cannot_overlap(tmp_path):
    from medical_rag.jobs import IngestLock

    with (
        IngestLock(tmp_path / "ingest.lock"),
        pytest.raises(RuntimeError),
        IngestLock(tmp_path / "ingest.lock"),
    ):
        pass


def test_snapshot_and_node_progress_are_persisted_with_injected_service(tmp_path):
    from medical_rag.jobs import DocumentProcessor
    from medical_rag.workflow.engine import WorkflowEngine
    from medical_rag.workflow.nodes.base import BuiltinNode
    from medical_rag.workflow.registry import NodeRegistry

    store = store_at(tmp_path)
    submitted = store.submit("a.txt", b"text")
    registry = NodeRegistry()
    stages = []

    def handler(inputs, config, context):
        public = store.get(submitted["job_id"])
        stages.append(public["stage"])
        if context.node_id == "chunk":
            assert config["max_tokens"] == store.settings.chunk_max_tokens
        return {}

    for node_type in ("input", "parse", "chunk", "embed", "store", "output"):
        registry.register(
            BuiltinNode("document." + node_type, node_type, "input", {}, {}, handler)
        )
    service = SimpleNamespace(engine=WorkflowEngine(registry))
    assert DocumentProcessor(store, service=service).process(submitted["job_id"])
    public = store.get(submitted["job_id"])
    assert public["status"] == "succeeded"
    assert {"parse", "chunk", "embed", "store"} <= set(stages)
    assert len(public["trace"]) == 6


def test_store_construction_is_lazy(tmp_path):
    store = store_at(tmp_path)
    assert not store.path.exists()


def test_public_failure_codes_and_trace_never_expose_adapter_text(tmp_path):
    from medical_rag.jobs import DocumentProcessor

    store = store_at(tmp_path)
    task = store.submit("a.txt", b"x")

    def fail(*args):
        raise ApiError("INVALID_DOCUMENT", "secret C:/private/upload.pdf", 422)

    DocumentProcessor(store, service=SimpleNamespace(ingest=fail)).process(
        task["job_id"]
    )
    assert store.get(task["job_id"])["error"]["code"] == "INVALID_DOCUMENT"
    assert "private" not in json.dumps(store.get(task["job_id"]))


def test_three_attempt_limit_and_dead_outbox(tmp_path):
    from medical_rag.jobs import DocumentProcessor

    store = store_at(tmp_path)
    task = store.submit("a.txt", b"x")

    def fail(*args):
        raise RuntimeError("secret")

    processor = DocumentProcessor(store, service=SimpleNamespace(ingest=fail))
    for seq in range(3):
        with store.connect(True) as db:
            db.execute("UPDATE document_jobs SET available_at=0")
        assert processor.process(task["job_id"], seq)
    public = store.get(task["job_id"])
    assert public["status"] == "failed" and public["attempts"] == 3
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM task_outbox WHERE destination='dead'"
            ).fetchone()[0]
            == 1
        )
    assert not list(store.upload_root.glob("*.upload"))


def test_health_degraded_for_stopped_and_stale_roles(tmp_path):
    store = store_at(tmp_path)
    assert store.health()["status"] == "degraded"
    for role in ("relay", "ingest", "memory"):
        store.heartbeat(role, "connected")
    assert store.health()["status"] == "ok"
    store.heartbeat("ingest", "stopped")
    assert store.health()["status"] == "degraded"
    store.heartbeat("ingest", "connected")
    with store.connect(True) as db:
        db.execute("UPDATE worker_heartbeats SET updated_at=0 WHERE role='memory'")
    assert store.health()["status"] == "degraded"


def test_published_snapshot_and_effective_settings_survive_later_edits(tmp_path):
    from medical_rag.jobs import DocumentJobStore, DocumentProcessor
    from medical_rag.workflow.repository import WorkflowRepository

    default = WorkflowRepository(
        Path(__file__).resolve().parents[1] / "workflows"
    ).get_published("document-ingestion")
    repository = WorkflowRepository(tmp_path / "workflows")
    repository.save(default)
    settings = Settings(
        chat_history_file=str(tmp_path / "history.db"),
        document_uploads_dir=str(tmp_path / "uploads"),
        collection_name="pinned-collection",
    )
    store = DocumentJobStore(settings, repository)
    task = store.submit("a.txt", b"x")
    modified = default.model_copy(deep=True, update={"version": 2})
    modified.nodes[2].config["max_tokens"] = 200
    repository.save(modified)
    settings.collection_name = "later-collection"
    received = []

    def factory(pinned_settings):
        received.append(pinned_settings.collection_name)
        return SimpleNamespace(ingest=lambda *args: {"chunks": 1})

    assert DocumentProcessor(store, service_factory=factory).process(task["job_id"])
    assert received == ["pinned-collection"]
    with store.connect() as db:
        snapshot = json.loads(
            db.execute("SELECT snapshot FROM document_jobs").fetchone()[0]
        )
    assert snapshot["workflow"]["version"] == 1
    assert (
        snapshot["workflow"]["nodes"][2]["config"]["max_tokens"]
        == settings.chunk_max_tokens
    )


def test_tampered_upload_location_cannot_read_or_delete_outside_root(tmp_path):
    from medical_rag.jobs import DocumentProcessor

    store = store_at(tmp_path)
    task = store.submit("a.txt", b"x")
    outside = tmp_path / "outside.upload"
    outside.write_bytes(b"private")
    with store.connect(True) as db:
        db.execute("UPDATE document_jobs SET upload_path=?", (str(outside),))
    calls = []
    assert DocumentProcessor(
        store, service=SimpleNamespace(ingest=lambda *args: calls.append(args))
    ).process(task["job_id"])
    assert store.get(task["job_id"])["status"] == "failed"
    assert not calls and outside.read_bytes() == b"private"
