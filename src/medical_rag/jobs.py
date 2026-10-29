"""SQLite-authoritative jobs and transactional, broker-independent task outbox."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .settings_store import resolve_path
from .types import ApiError
from .workflow.repository import WorkflowRepository

logger = logging.getLogger(__name__)
TERMINAL = ("succeeded", "failed")
SAFE_ERRORS = {
    "INVALID_DOCUMENT": "文档内容或格式无效",
    "INVALID_SOURCE": "资料来源标识无效",
    "DEPENDENCY_ERROR": "入库依赖服务暂时不可用",
    "MODEL_RATE_LIMITED": "模型服务请求过于频繁",
    "MODEL_UPSTREAM_ERROR": "模型服务暂时不可用",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS document_jobs (
 job_order INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL,
 filename TEXT NOT NULL, source_id TEXT NOT NULL, upload_path TEXT NOT NULL,
 snapshot TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, dispatch_seq INTEGER NOT NULL DEFAULT 0,
 available_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0, token TEXT,
 result TEXT, error TEXT, trace TEXT NOT NULL DEFAULT '[]',
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS document_source_order ON document_jobs(source_id,job_order,status);
CREATE TABLE IF NOT EXISTS task_outbox (
 message_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, task_id TEXT NOT NULL,
 dispatch_seq INTEGER NOT NULL, destination TEXT NOT NULL, payload TEXT NOT NULL,
 available_at REAL NOT NULL, sent_at REAL, lease_until REAL NOT NULL DEFAULT 0,
 token TEXT, UNIQUE(task_type,task_id,dispatch_seq,destination));
CREATE INDEX IF NOT EXISTS outbox_due ON task_outbox(sent_at,available_at,lease_until);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
 role TEXT PRIMARY KEY, updated_at REAL NOT NULL, broker TEXT NOT NULL);
"""


def initialize_outbox(connection: sqlite3.Connection) -> None:
    """Create shared tables without executescript's implicit transaction commit."""
    for statement in SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)


def enqueue_task(
    connection: sqlite3.Connection,
    task_type: str,
    task_id: str,
    dispatch_seq: int = 0,
    available_at: float | None = None,
    dead: bool = False,
) -> None:
    """Append one durable dispatch in the caller's transaction/savepoint."""
    message_id = uuid4().hex
    payload = {
        "schema_version": 1,
        "message_id": message_id,
        "task_type": task_type,
        "task_id": task_id,
        "dispatch_seq": dispatch_seq,
        "trace_id": task_id,
    }
    connection.execute(
        "INSERT OR IGNORE INTO task_outbox(message_id,task_type,task_id,dispatch_seq,destination,payload,available_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            message_id,
            task_type,
            task_id,
            dispatch_seq,
            "dead" if dead else "tasks",
            json.dumps(payload),
            time.time() if available_at is None else available_at,
        ),
    )


def safe_trace(trace: list) -> list:
    """Persist operational trace only, never arbitrary adapter text or paths."""
    result = []
    for entry in trace:
        item = asdict(entry) if is_dataclass(entry) else dict(entry)
        item["metadata"] = {
            key: value
            for key, value in item.get("metadata", {}).items()
            if isinstance(value, (bool, int, float))
        }
        if item.get("error"):
            code = item["error"].get("code", "DEPENDENCY_ERROR")
            if code not in SAFE_ERRORS:
                code = "DEPENDENCY_ERROR"
            item["error"] = {"code": code, "message": SAFE_ERRORS[code]}
        result.append(item)
    return result


class DocumentJobStore:
    """Accept uploads without loading models, Milvus, or a broker connection."""

    def __init__(
        self, settings: Settings, repository: WorkflowRepository | None = None
    ) -> None:
        self.settings = settings
        self.path = resolve_path(settings.chat_history_file)
        self.upload_root = resolve_path(settings.document_uploads_dir).resolve()
        self.repository = repository or WorkflowRepository(
            Path(__file__).resolve().parents[2] / "workflows"
        )

    @contextmanager
    def connect(self, write: bool = False):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with (
                closing(sqlite3.connect(self.path, timeout=30)) as connection,
                connection,
            ):
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=30000")
                initialize_outbox(connection)
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        except (OSError, sqlite3.Error) as error:
            raise ApiError(
                "JOB_STORAGE_ERROR", "任务存储不可用，请检查磁盘和权限", 503
            ) from error

    def submit(
        self, filename: str, content: bytes, source_id: str | None = None
    ) -> dict:
        if (
            not isinstance(filename, str)
            or not filename.strip()
            or len(filename.encode("utf-8")) > 512
            or filename.rsplit(".", 1)[-1].lower() not in {"txt", "md", "pdf"}
            or not isinstance(content, bytes)
            or not content
        ):
            raise ApiError(
                "INVALID_DOCUMENT", "请上传非空的 PDF、Markdown 或文本文件", 422
            )
        if len(content) > self.settings.max_upload_bytes:
            raise ApiError("FILE_TOO_LARGE", "上传文件超过大小限制", 413)
        if source_id is not None and (
            not isinstance(source_id, str)
            or not source_id.strip()
            or len(source_id.encode("utf-8")) > 512
        ):
            raise ApiError("INVALID_SOURCE", "资料来源标识无效", 422)
        try:
            definition = self.repository.get_published("document-ingestion")
        except (KeyError, ValueError) as error:
            raise ApiError(
                "WORKFLOW_NOT_PUBLISHED", "入库工作流尚未发布", 409
            ) from error
        snapshot = {
            "workflow": definition.model_dump(mode="json"),
            "settings": {
                key: getattr(self.settings, key)
                for key in (
                    "chunk_max_tokens",
                    "chunk_overlap_tokens",
                    "parent_max_tokens",
                    "collection_name",
                )
            },
        }
        for node in snapshot["workflow"]["nodes"]:
            if node["type"] == "document.chunk":
                for config_key, setting_key in (
                    ("max_tokens", "chunk_max_tokens"),
                    ("overlap_tokens", "chunk_overlap_tokens"),
                    ("parent_max_tokens", "parent_max_tokens"),
                ):
                    node["config"].setdefault(
                        config_key, snapshot["settings"][setting_key]
                    )
        job_id = uuid4().hex
        path = self.upload_root / (job_id + ".upload")
        temporary = None
        try:
            self.upload_root.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".upload-", dir=self.upload_root
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            now = time.time()
            with self.connect(True) as connection:
                connection.execute(
                    "INSERT INTO document_jobs(job_id,filename,source_id,upload_path,snapshot,status,stage,"
                    "available_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        filename,
                        source_id if source_id else filename,
                        str(path),
                        json.dumps(snapshot),
                        "queued",
                        "queued",
                        now,
                        now,
                        now,
                    ),
                )
                enqueue_task(connection, "document.ingest", job_id)
        except (OSError, ApiError) as error:
            path.unlink(missing_ok=True)
            raise ApiError(
                "JOB_STORAGE_ERROR", "上传保存失败，请检查磁盘和权限", 503
            ) from error
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        return {
            "job_id": job_id,
            "status": "queued",
            "status_url": "/document-jobs/" + job_id,
        }

    @staticmethod
    def public(row: sqlite3.Row) -> dict:
        keys = (
            "job_id",
            "filename",
            "source_id",
            "status",
            "stage",
            "attempts",
            "created_at",
            "updated_at",
        )
        return {
            **{key: row[key] for key in keys},
            "result": json.loads(row["result"]) if row["result"] else None,
            "error": json.loads(row["error"]) if row["error"] else None,
            "trace": json.loads(row["trace"]),
        }

    def get(self, job_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM document_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ApiError("JOB_NOT_FOUND", "任务不存在", 404)
            return self.public(row)

    def list_jobs(self, active: bool = False, limit: int = 50) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM document_jobs "
                + ("WHERE status NOT IN ('succeeded','failed') " if active else "")
                + "ORDER BY job_order DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
            return [self.public(row) for row in rows]

    def claim(self, job_id: str, dispatch_seq: int) -> dict | None:
        with self.connect(True) as connection:
            now = time.time()
            row = connection.execute(
                "SELECT * FROM document_jobs j WHERE job_id=? AND dispatch_seq=? AND status IN ('queued','retrying') "
                "AND available_at<=? AND NOT EXISTS (SELECT 1 FROM document_jobs p WHERE p.source_id=j.source_id "
                "AND p.job_order<j.job_order AND p.status NOT IN ('succeeded','failed'))",
                (job_id, dispatch_seq, now),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            connection.execute(
                "UPDATE document_jobs SET status='running',stage='loading',attempts=attempts+1,"
                "token=?,lease_until=?,updated_at=? WHERE job_id=?",
                (token, now + 60, now, job_id),
            )
            return {**dict(row), "token": token, "attempts": row["attempts"] + 1}

    def progress(
        self, job_id: str, token: str, stage: str, trace: list | None = None
    ) -> None:
        with self.connect(True) as connection:
            connection.execute(
                "UPDATE document_jobs SET stage=?,trace=COALESCE(?,trace),lease_until=?,updated_at=? "
                "WHERE job_id=? AND token=? AND status='running'",
                (
                    stage,
                    json.dumps(safe_trace(trace)) if trace is not None else None,
                    time.time() + 60,
                    time.time(),
                    job_id,
                    token,
                ),
            )

    def finish(
        self,
        job: dict,
        result: dict | None = None,
        error: Exception | None = None,
        trace: list | None = None,
    ) -> None:
        terminal = (
            error is None
            or isinstance(error, (ValueError, FileNotFoundError))
            or (isinstance(error, ApiError) and error.status < 500)
            or job["attempts"] >= 3
        )
        status = ("failed" if error else "succeeded") if terminal else "retrying"
        safe_error = None
        if error:
            code = (
                error.code
                if isinstance(error, ApiError) and error.code in SAFE_ERRORS
                else "DEPENDENCY_ERROR"
            )
            if isinstance(error, (ValueError, FileNotFoundError)):
                code = "INVALID_DOCUMENT"
            safe_error = {"code": code, "message": SAFE_ERRORS[code]}
        now = time.time()
        available = now + (10 if job["attempts"] == 1 else 60)
        with self.connect(True) as connection:
            changed = connection.execute(
                "UPDATE document_jobs SET status=?,stage=?,result=?,error=?,trace=COALESCE(?,trace),"
                "token=NULL,lease_until=0,updated_at=?,available_at=?,dispatch_seq=dispatch_seq+? "
                "WHERE job_id=? AND token=? AND status='running'",
                (
                    status,
                    status,
                    json.dumps(result, ensure_ascii=False)
                    if result is not None
                    else None,
                    json.dumps(safe_error) if safe_error else None,
                    json.dumps(safe_trace(trace)) if trace is not None else None,
                    now,
                    available,
                    int(not terminal),
                    job["job_id"],
                    job["token"],
                ),
            ).rowcount
            if changed and error:
                enqueue_task(
                    connection,
                    "document.ingest",
                    job["job_id"],
                    job["dispatch_seq"] + int(not terminal),
                    available_at=now if terminal else available,
                    dead=terminal,
                )
        if terminal and changed:
            self.remove_upload(job["upload_path"])

    def remove_upload(self, value: str) -> None:
        path = Path(value)
        if (
            path.parent.resolve() != self.upload_root
            or path.is_symlink()
            or path.suffix != ".upload"
        ):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Terminal upload cleanup pending")

    def cleanup_orphans(self) -> int:
        """Best-effort maintenance; one inaccessible file must not stall dispatch."""
        with self.connect() as connection:
            referenced = {
                row[0]
                for row in connection.execute("SELECT upload_path FROM document_jobs")
            }
            terminal = [
                row[0]
                for row in connection.execute(
                    "SELECT upload_path FROM document_jobs WHERE status IN ('failed','succeeded')"
                )
            ]
        for value in terminal:
            try:
                self.remove_upload(value)
            except OSError:
                logger.warning("Terminal upload cleanup pending")
        removed = 0
        try:
            paths = list(self.upload_root.iterdir())
        except FileNotFoundError:
            return removed
        except OSError:
            logger.warning("Upload directory cleanup unavailable")
            return removed
        for path in paths:
            try:
                if str(path) in referenced or path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_mtime < time.time() - 86400 and (
                    path.suffix == ".upload" or path.name.startswith(".upload-")
                ):
                    path.unlink()
                    removed += 1
            except OSError:
                logger.warning("Orphan upload cleanup pending")
        return removed

    def heartbeat(self, role: str, broker: str = "unknown") -> None:
        with self.connect(True) as connection:
            connection.execute(
                "INSERT INTO worker_heartbeats VALUES (?,?,?) ON CONFLICT(role) "
                "DO UPDATE SET updated_at=excluded.updated_at,broker=excluded.broker",
                (role, time.time(), broker),
            )

    def health(self) -> dict:
        with self.connect() as connection:
            counts = {
                state: 0
                for state in ("queued", "running", "retrying", "succeeded", "failed")
            }
            counts.update(
                {
                    row[0]: row[1]
                    for row in connection.execute(
                        "SELECT status,count(*) FROM document_jobs GROUP BY status"
                    )
                }
            )
            oldest = connection.execute(
                "SELECT min(created_at) FROM document_jobs WHERE status IN ('queued','retrying')"
            ).fetchone()[0]
            beats = {
                row["role"]: dict(row)
                for row in connection.execute("SELECT * FROM worker_heartbeats")
            }
            pending = connection.execute(
                "SELECT count(*) FROM task_outbox WHERE sent_at IS NULL"
            ).fetchone()[0]
        now = time.time()
        roles = {
            role: {
                "age_seconds": max(0, now - beats[role]["updated_at"])
                if role in beats
                else None,
                "stale": role not in beats
                or now - beats[role]["updated_at"] > 30
                or beats[role]["broker"] == "stopped",
            }
            for role in ("relay", "ingest", "memory")
        }
        return {
            "status": "ok"
            if all(
                not roles[role]["stale"] and beats[role]["broker"] == "connected"
                for role in roles
            )
            else "degraded",
            "counts": counts,
            "oldest_wait_seconds": max(0, now - oldest) if oldest else 0,
            "outbox_pending": pending,
            "roles": roles,
            "broker": beats.get("relay", {}).get("broker", "unknown")
            if not roles["relay"]["stale"]
            else "unknown",
        }


class IngestLock:
    """OS-held singleton lock: lease expiry never permits overlapping writers."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                if self.file.read(1) == b"":
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise RuntimeError(
                "An ingestion worker already owns this database"
            ) from error
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()


class DocumentProcessor:
    """Synchronous document execution; consumers must run this off their AMQP loop."""

    def __init__(
        self,
        store: DocumentJobStore,
        service: object | None = None,
        service_factory=None,
    ) -> None:
        self.store = store
        self.service = service
        self.service_factory = service_factory

    def process(self, job_id: str, dispatch_seq: int = 0) -> bool:
        job = self.store.claim(job_id, dispatch_seq)
        if job is None:
            return False
        stop = threading.Event()
        started = time.monotonic()

        def renew():
            warned = False
            while not stop.wait(10):
                try:
                    with self.store.connect(True) as connection:
                        connection.execute(
                            "UPDATE document_jobs SET lease_until=? WHERE job_id=? AND token=? AND status='running'",
                            (time.time() + 60, job_id, job["token"]),
                        )
                    if not warned and time.monotonic() - started > 900:
                        logger.warning(
                            "Document ingestion has exceeded fifteen minutes"
                        )
                        warned = True
                except ApiError:
                    logger.warning("Document ingestion lease renewal unavailable")

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        trace = []
        try:
            path = Path(job["upload_path"])
            if path.parent.resolve() != self.store.upload_root or path.is_symlink():
                raise ValueError("Invalid upload location")
            content = path.read_bytes()
            snapshot = json.loads(job["snapshot"])
            settings = self.store.settings.model_copy(update=snapshot["settings"])
            service = self.service or (self.service_factory or build_ingest_service)(
                settings
            )
            self.store.progress(job_id, job["token"], "ingesting")
            if hasattr(service, "engine"):
                from .workflow.definition import WorkflowDefinition
                from .workflow.engine import WorkflowEngine

                engine = service.engine
                if hasattr(engine, "registry"):
                    engine = WorkflowEngine(
                        _ProgressRegistry(engine.registry, self.store, job),
                        getattr(engine, "capabilities", {}),
                    )
                run = asyncio.run(
                    engine.run(
                        WorkflowDefinition.model_validate(snapshot["workflow"]),
                        {
                            "filename": job["filename"],
                            "content": content,
                            "source_id": job["source_id"],
                        },
                    )
                )
                trace = [
                    asdict(item) if is_dataclass(item) else dict(item)
                    for item in run.trace
                ]
                if run.status != "success":
                    failure = (
                        next(
                            (
                                item.get("error")
                                for item in trace
                                if item.get("status") == "failed"
                            ),
                            {},
                        )
                        or {}
                    )
                    code = failure.get("code", "DEPENDENCY_ERROR")
                    raise ApiError(
                        "DEPENDENCY_ERROR" if code == "NODE_EXECUTION_ERROR" else code,
                        "文档入库失败",
                        422
                        if failure.get("code") in {"INVALID_DOCUMENT", "INVALID_SOURCE"}
                        else 503,
                    )
                result = run.output
            else:
                result = service.ingest(job["filename"], content, job["source_id"])
            self.store.finish(job, result=result, trace=trace)
        except Exception as error:  # noqa: BLE001 - commit safe failure before broker acknowledgement
            self.store.finish(job, error=error, trace=trace)
        finally:
            stop.set()
            thread.join()
        return True


def build_ingest_service(settings: Settings):
    """Heavy dependencies are loaded only inside the ingest worker execution."""
    from pymilvus import MilvusClient

    from .embeddings import BgeEmbedder
    from .service import RagService
    from .store import MilvusStore

    store = MilvusStore(
        MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or None),
        settings.collection_name,
    )
    store.ensure_schema()
    return RagService(settings, store, BgeEmbedder(), None, None)


class _ProgressRegistry:
    """Per-run node proxies keep an injected service's shared registry untouched."""

    def __init__(self, registry, store: DocumentJobStore, job: dict):
        self.registry, self.store, self.job = registry, store, job

    def get(self, node_type: str):
        node = self.registry.get(node_type)
        store, job = self.store, self.job

        class ProgressNode:
            def __getattr__(self, name):
                return getattr(node, name)

            async def execute(self, inputs, config, context):
                stage = (
                    node_type.removeprefix("document.")
                    if node_type.startswith("document.")
                    else "ingesting"
                )
                store.progress(
                    job["job_id"],
                    job["token"],
                    stage,
                    [asdict(item) for item in context.trace],
                )
                return await node.execute(inputs, config, context)

        return ProgressNode()
