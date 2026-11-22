"""Read-only access to the local artifacts the Streamlit viewer displays.

Everything here opens ``data/chat-history.sqlite3`` with SQLite's ``mode=ro``
URI: the viewer must never create tables, migrate or write a single row. Corpus
evaluation reports are plain JSON files, so they are parsed and validated before
being shown. Failures are contained: a missing file, a missing table or one
corrupt report yields an empty/partial view instead of an exception page.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_REPORT_LIMIT = 100
MAX_REPORT_FILE_BYTES = 200 * 1024 * 1024

# Metrics worth putting on one screen. Reports may lack any of them (an aborted
# evaluation has an empty summary), so the viewer only prints present numbers.
REPORT_METRICS: tuple[tuple[str, str], ...] = (
    ("precision", "检索 Precision"),
    ("recall", "检索 Recall"),
    ("ndcg", "检索 NDCG"),
    ("mrr", "检索 MRR"),
    ("hit_rate", "检索 Hit Rate"),
    ("faithfulness", "忠实度"),
    ("answer_relevance", "答案相关性"),
    ("context_relevance", "上下文相关性"),
    ("abstention_rate", "拒答率"),
    ("correct_abstention_rate", "正确拒答率"),
    ("judge_failures", "裁判失败数"),
)


def project_path(value: str | Path) -> Path:
    """Resolve a configured path against the project root, not the CWD."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def parse_report_dirs(value: str) -> list[Path]:
    """Split a comma/semicolon separated report-directory list, keeping order."""
    seen: set[str] = set()
    result: list[Path] = []
    for part in value.replace(os.pathsep, ",").split(","):
        text = part.strip()
        if not text:
            continue
        path = project_path(text)
        key = str(path.resolve()) if path.exists() else os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def format_timestamp(value: float | int | None) -> str:
    """Local wall-clock rendering for SQLite/epoch timestamps."""
    if value is None:
        return "—"
    try:
        moment = datetime.fromtimestamp(float(value), tz=UTC).astimezone()
    except (OverflowError, OSError, ValueError):
        return "—"
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def format_bytes(size: int | None) -> str:
    if not size:
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"  # pragma: no cover - unreachable


# -- conversations ---------------------------------------------------------
@dataclass(frozen=True)
class Message:
    """One stored chat message; assistant answers keep their evidence links."""

    role: str
    content: str
    citations: tuple[dict[str, Any], ...] = ()
    abstained: bool | None = None
    abstain_reason: str | None = None
    answer_type: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ConversationIndex:
    """Lightweight row used by the conversation list."""

    conversation_id: str
    title: str
    updated: float
    turn_count: int
    answered_count: int
    last_question: str


@dataclass(frozen=True)
class Conversation:
    """One conversation with its legacy messages and durable turns."""

    conversation_id: str
    title: str
    updated: float
    messages: tuple[Message, ...]
    failed_runs: tuple[dict[str, Any], ...]


@contextmanager
def _open_readonly(path: Path) -> Iterator[sqlite3.Connection | None]:
    """Open ``path`` read-only; ``None`` when it does not exist or is unreadable."""
    if not path.is_file():
        yield None
        return
    uri = f"{path.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error:
        yield None
        return
    try:
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def load_conversation_index(history_file: str | Path, limit: int = 200) -> list[ConversationIndex]:
    """Conversations, most recently updated first, without loading every message."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit 必须是正整数")
    path = project_path(history_file)
    with _open_readonly(path) as connection:
        if connection is None or not _table_exists(connection, "conversations"):
            return []
        try:
            rows = connection.execute(
                "SELECT c.id, c.title, c.updated, "
                "  (SELECT COUNT(*) FROM turns t WHERE t.conversation_id = c.id) AS turn_count, "
                "  (SELECT COUNT(*) FROM turns t WHERE t.conversation_id = c.id "
                "     AND t.answer IS NOT NULL) AS answered_count, "
                "  (SELECT t.question FROM turns t WHERE t.conversation_id = c.id "
                "     ORDER BY t.id DESC LIMIT 1) AS last_question "
                "FROM conversations c ORDER BY c.updated DESC, c.id LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
    return [
        ConversationIndex(
            conversation_id=str(row["id"]),
            title=str(row["title"] or "未命名会话"),
            updated=float(row["updated"] or 0),
            turn_count=int(row["turn_count"] or 0),
            answered_count=int(row["answered_count"] or 0),
            last_question=str(row["last_question"] or ""),
        )
        for row in rows
    ]


def _parse_answer(raw: str | None) -> Message | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    citations = payload.get("citations")
    return Message(
        role="assistant",
        content=content,
        citations=tuple(item for item in citations if isinstance(item, dict))
        if isinstance(citations, list) else (),
        abstained=payload.get("abstained") if isinstance(payload.get("abstained"), bool) else None,
        abstain_reason=payload.get("abstainReason") if isinstance(payload.get("abstainReason"), str) else None,
        answer_type=payload.get("answerType") if isinstance(payload.get("answerType"), str) else None,
        run_id=payload.get("runId") if isinstance(payload.get("runId"), str) else None,
    )


def _legacy_messages(raw: str | None) -> list[Message]:
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    messages: list[Message] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        citations = item.get("citations")
        messages.append(Message(
            role=role,
            content=content,
            citations=tuple(entry for entry in citations if isinstance(entry, dict))
            if isinstance(citations, list) else (),
            abstained=item.get("abstained") if isinstance(item.get("abstained"), bool) else None,
            abstain_reason=item.get("abstainReason") if isinstance(item.get("abstainReason"), str) else None,
            answer_type=item.get("answerType") if isinstance(item.get("answerType"), str) else None,
            run_id=item.get("runId") if isinstance(item.get("runId"), str) else None,
        ))
    return messages


def load_conversation(history_file: str | Path,
                      conversation_id: str) -> Conversation | None:
    """Load exactly one conversation, merging imported legacy messages and turns."""
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("conversation_id 不能为空")
    path = project_path(history_file)
    with _open_readonly(path) as connection:
        if connection is None or not _table_exists(connection, "conversations"):
            return None
        try:
            row = connection.execute("SELECT * FROM conversations WHERE id = ?",
                                     (conversation_id,)).fetchone()
            if row is None:
                return None
            turns = connection.execute(
                "SELECT question, answer, failure FROM turns WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        except sqlite3.Error:
            return None
        messages = _legacy_messages(row["legacy"])
        failed_runs = _legacy_failures(row["failed_runs"])
    for turn in turns:
        question = turn["question"]
        if isinstance(question, str) and question.strip():
            messages.append(Message(role="user", content=question))
        answer = _parse_answer(turn["answer"])
        if answer is not None:
            messages.append(answer)
        failed_runs.extend(_legacy_failures(turn["failure"]))
    return Conversation(
        conversation_id=str(row["id"]),
        title=str(row["title"] or "未命名会话"),
        updated=float(row["updated"] or 0),
        messages=tuple(messages),
        failed_runs=tuple(failed_runs),
    )


def _legacy_failures(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


# -- documents and workers -------------------------------------------------
def load_document_jobs(history_file: str | Path, limit: int = 100) -> list[dict[str, Any]]:
    """Newest ingestion jobs first; the table is created by the legacy workers."""
    path = project_path(history_file)
    with _open_readonly(path) as connection:
        if connection is None or not _table_exists(connection, "document_jobs"):
            return []
        try:
            rows = connection.execute(
                "SELECT job_id, filename, source_id, status, stage, attempts, error, "
                "       created_at, updated_at FROM document_jobs "
                "ORDER BY job_order DESC LIMIT ?", (limit,),
            ).fetchall()
        except sqlite3.Error:
            return []
    return [dict(row) for row in rows]


def load_worker_heartbeats(history_file: str | Path) -> list[dict[str, Any]]:
    """Latest heartbeat per worker role, if the workers ever ran on this host."""
    path = project_path(history_file)
    with _open_readonly(path) as connection:
        if connection is None or not _table_exists(connection, "worker_heartbeats"):
            return []
        try:
            rows = connection.execute(
                "SELECT role, updated_at, broker FROM worker_heartbeats ORDER BY role"
            ).fetchall()
        except sqlite3.Error:
            return []
    return [dict(row) for row in rows]


@dataclass(frozen=True)
class DatabaseOverview:
    """File-level facts plus the few counts that stay cheap on a large database."""

    path: Path
    exists: bool
    size_bytes: int
    modified_at: float | None
    conversation_count: int
    turn_count: int
    answered_count: int
    document_job_count: int
    pending_job_count: int


def load_database_overview(history_file: str | Path) -> DatabaseOverview:
    """Read-only inventory used by the overview page."""
    path = project_path(history_file)
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    modified = path.stat().st_mtime if exists else None
    counts = {"conversations": 0, "turns": 0, "answered": 0, "jobs": 0, "pending": 0}
    with _open_readonly(path) as connection:
        if connection is not None:
            try:
                if _table_exists(connection, "conversations"):
                    counts["conversations"] = int(connection.execute(
                        "SELECT COUNT(*) FROM conversations").fetchone()[0])
                if _table_exists(connection, "turns"):
                    counts["turns"] = int(connection.execute(
                        "SELECT COUNT(*) FROM turns").fetchone()[0])
                    counts["answered"] = int(connection.execute(
                        "SELECT COUNT(*) FROM turns WHERE answer IS NOT NULL").fetchone()[0])
                if _table_exists(connection, "document_jobs"):
                    counts["jobs"] = int(connection.execute(
                        "SELECT COUNT(*) FROM document_jobs").fetchone()[0])
                    counts["pending"] = int(connection.execute(
                        "SELECT COUNT(*) FROM document_jobs WHERE status NOT IN "
                        "('succeeded','failed','cancelled')").fetchone()[0])
            except sqlite3.Error:
                pass
    return DatabaseOverview(
        path=path,
        exists=exists,
        size_bytes=size,
        modified_at=modified,
        conversation_count=counts["conversations"],
        turn_count=counts["turns"],
        answered_count=counts["answered"],
        document_job_count=counts["jobs"],
        pending_job_count=counts["pending"],
    )


# -- evaluation reports ----------------------------------------------------
@dataclass(frozen=True)
class ReportFile:
    """A parsed evaluation report, reduced to what the list page shows."""

    path: Path
    modified: float
    case_count: int
    dataset_version: str | None
    answer_provider: str | None
    judge_model: str | None
    summary: dict[str, Any]


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _report_meta(path: Path, payload: dict[str, Any]) -> ReportFile | None:
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return None
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    cases = payload.get("cases")
    return ReportFile(
        path=path,
        modified=path.stat().st_mtime,
        case_count=len(cases) if isinstance(cases, list) else _int_value(summary.get("case_count")),
        dataset_version=payload.get("dataset_version") if isinstance(payload.get("dataset_version"), str) else None,
        answer_provider=config.get("answer_provider") if isinstance(config.get("answer_provider"), str) else None,
        judge_model=config.get("judge_model") if isinstance(config.get("judge_model"), str) else None,
        summary=summary,
    )


def read_report(path: str | Path) -> dict[str, Any]:
    """Parse one evaluation report, raising ``ValueError`` when it is not one."""
    target = project_path(path)
    if not target.is_file():
        raise ValueError("报告文件不存在")
    size = target.stat().st_size
    if size > MAX_REPORT_FILE_BYTES:
        raise ValueError("报告文件过大，无法在界面中打开")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("报告文件不是有效的 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("报告文件结构无效")
    return payload


def scan_reports(report_dirs: str | list[str] | list[Path],
                 limit: int = DEFAULT_REPORT_LIMIT) -> list[ReportFile]:
    """Scan configured directories for report JSON files, newest first.

    Only files that actually look like an evaluation report (a top-level
    ``summary`` object) are returned; anything else is silently skipped.
    """
    if isinstance(report_dirs, str):
        directories = parse_report_dirs(report_dirs)
    else:
        directories = [project_path(item) for item in report_dirs]
    found: list[ReportFile] = []
    seen: set[str] = set()
    candidates: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        try:
            candidates.extend(
                item for item in directory.rglob("*.json") if item.is_file()
            )
        except OSError:
            continue
    for candidate in sorted(candidates, key=_safe_mtime, reverse=True):
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        meta = _report_meta(candidate, payload)
        if meta is not None:
            found.append(meta)
        if len(found) >= limit:
            break
    return found


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def report_metrics(report: dict[str, Any] | ReportFile) -> list[tuple[str, str, float]]:
    """Present numeric summary metrics as ``(key, label, value)`` triples."""
    summary = report.summary if isinstance(report, ReportFile) else report.get("summary")
    if not isinstance(summary, dict):
        return []
    result: list[tuple[str, str, float]] = []
    for key, label in REPORT_METRICS:
        value = summary.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        result.append((key, label, value))
    return result


def report_case_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten case results for a filterable table; malformed cases are skipped."""
    cases = report.get("cases")
    if not isinstance(cases, list):
        return []
    rows: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        retrieval = case.get("retrieval") if isinstance(case.get("retrieval"), dict) else {}
        answer = case.get("answer") if isinstance(case.get("answer"), dict) else {}
        rows.append({
            "题目 ID": case.get("id"),
            "问题": case.get("question"),
            "类别": case.get("category"),
            "可答性": case.get("answerability"),
            "拒答": case.get("abstained"),
            "Recall": retrieval.get("recall"),
            "NDCG": retrieval.get("ndcg"),
            "MRR": retrieval.get("rr"),
            "错误": case.get("error"),
            "答案摘录": (answer.get("answer") or "")[:120] if isinstance(answer.get("answer"), str) else "",
        })
    return rows


# -- local backend health probe -------------------------------------------
def probe_backend_health(base_url: str, timeout: float = 1.5) -> dict[str, Any]:
    """Best-effort read of the local RAG ``/health`` endpoint.

    Only loopback origins are accepted: the viewer is a local operations page and
    must not become an unauthenticated probe for arbitrary remote hosts.
    """
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or parts.hostname not in LOOPBACK_HOSTS:
        return {"reachable": False, "error": "仅允许探测本机回环 HTTP 地址", "payload": None}
    url = base_url.rstrip("/") + "/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            raw = response.read(256 * 1024)
            status = response.status
    except urllib.error.HTTPError as error:
        return {"reachable": False, "error": f"HTTP {error.code}", "payload": None}
    except (urllib.error.URLError, OSError, ValueError) as error:
        return {"reachable": False, "error": f"后端不可达: {type(error).__name__}", "payload": None}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        payload = None
    return {"reachable": 200 <= status < 300, "error": None if 200 <= status < 300 else f"HTTP {status}",
            "payload": payload if isinstance(payload, dict) else None}
