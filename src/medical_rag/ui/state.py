"""Durable UI state for the Streamlit viewer.

Streamlit throws ``session_state`` away when the process restarts, so the page,
the last selected conversation/report and operator notes are written to SQLite
instead. This module is the only writer in the viewer: the chat database is
opened read-only by :mod:`medical_rag.ui.data`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..settings_store import resolve_path

PAGE_KEY = "ui.page"
CONVERSATION_KEY = "ui.conversation"
REPORT_KEY = "ui.report"
NOTE_KINDS = frozenset({"conversation", "report", "document"})
MAX_KEY_CHARS = 200
MAX_NOTE_CHARS = 4000
MAX_TARGET_ID_CHARS = 512
MAX_VALUE_BYTES = 64 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS ui_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ui_notes (
    note_id TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ui_notes_target ON ui_notes(target_kind, target_id, updated);
"""


class UiStateError(RuntimeError):
    """Raised when the persistent UI state cannot be read or written."""


def _require_text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 不能为空")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{name} 超过 {limit} 字符上限")
    return text


def _note(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "note_id": row["note_id"],
        "target_kind": row["target_kind"],
        "target_id": row["target_id"],
        "body": row["body"],
        "created": float(row["created"]),
        "updated": float(row["updated"]),
    }


class UiStateStore:
    """Small SQLite key/value store plus notes, shared by every viewer session.

    Every public method opens its own short-lived connection, so the store can be
    reused from a cached Streamlit resource and from a second browser tab without
    sharing connection state.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = resolve_path(path)
        self._ready = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=15)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 5000")
                if not self._ready:
                    connection.executescript(SCHEMA)
                    self._ready = True
                yield connection
        except (sqlite3.Error, OSError) as error:
            self._ready = False
            raise UiStateError(
                "界面状态无法读写，请检查 UI_STATE_FILE 的路径、权限和磁盘空间"
            ) from error

    # -- navigation and view preferences ----------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """Return one stored JSON value, or ``default`` when absent/unreadable."""
        name = _require_text(key, "状态键", MAX_KEY_CHARS)
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM ui_state WHERE key = ?", (name,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            # A hand-edited or truncated row must not take the whole viewer down.
            return default

    def set(self, key: str, value: Any) -> None:
        """Persist one JSON-serialisable value, replacing any previous one."""
        name = _require_text(key, "状态键", MAX_KEY_CHARS)
        try:
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("界面状态值必须是可序列化的 JSON 数据") from error
        if len(payload.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ValueError(f"界面状态值超过 {MAX_VALUE_BYTES} 字节上限")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO ui_state(key, value, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (name, payload, time.time()),
            )
            connection.commit()

    def delete(self, key: str) -> bool:
        """Remove one stored value; returns whether a row existed."""
        name = _require_text(key, "状态键", MAX_KEY_CHARS)
        with self._connect() as connection:
            changed = connection.execute("DELETE FROM ui_state WHERE key = ?", (name,)).rowcount
            connection.commit()
        return bool(changed)

    def snapshot(self) -> dict[str, Any]:
        """Every stored value, used by the "persisted state" sidebar panel."""
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM ui_state ORDER BY key").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
        return result

    def reset_navigation(self) -> None:
        """Forget page/selection so the next start shows the default overview."""
        with self._connect() as connection:
            connection.execute("DELETE FROM ui_state WHERE key IN (?, ?, ?)",
                               (PAGE_KEY, CONVERSATION_KEY, REPORT_KEY))
            connection.commit()

    # -- operator notes ----------------------------------------------------
    def save_note(self, target_kind: str, target_id: str, body: str,
                  note_id: str | None = None) -> dict[str, Any]:
        """Create a note or replace its body; notes survive viewer restarts."""
        if target_kind not in NOTE_KINDS:
            raise ValueError("笔记类型无效")
        target = _require_text(target_id, "笔记对象", MAX_TARGET_ID_CHARS)
        text = _require_text(body, "笔记内容", MAX_NOTE_CHARS)
        now = time.time()
        with self._connect() as connection:
            if note_id:
                existing = connection.execute(
                    "SELECT created FROM ui_notes WHERE note_id = ?", (note_id,)).fetchone()
                if existing is None:
                    raise UiStateError("要更新的笔记不存在")
                connection.execute(
                    "UPDATE ui_notes SET body = ?, target_kind = ?, target_id = ?, updated = ? "
                    "WHERE note_id = ?",
                    (text, target_kind, target, now, note_id),
                )
            else:
                note_id = uuid4().hex
                connection.execute(
                    "INSERT INTO ui_notes(note_id, target_kind, target_id, body, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (note_id, target_kind, target, text, now, now),
                )
            row = connection.execute(
                "SELECT * FROM ui_notes WHERE note_id = ?", (note_id,)).fetchone()
            connection.commit()
        if row is None:  # pragma: no cover - defensive, the write just succeeded
            raise UiStateError("笔记保存失败")
        return _note(row)

    def list_notes(self, target_kind: str | None = None,
                   target_id: str | None = None) -> list[dict[str, Any]]:
        """Newest first, optionally narrowed to one kind or one target."""
        if target_kind is not None and target_kind not in NOTE_KINDS:
            raise ValueError("笔记类型无效")
        sql = "SELECT * FROM ui_notes"
        clauses: list[str] = []
        params: list[str] = []
        if target_kind is not None:
            clauses.append("target_kind = ?")
            params.append(target_kind)
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(_require_text(target_id, "笔记对象", MAX_TARGET_ID_CHARS))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_note(row) for row in rows]

    def delete_note(self, note_id: str) -> bool:
        """Delete one note; returns whether it existed."""
        name = _require_text(note_id, "笔记 ID", MAX_KEY_CHARS)
        with self._connect() as connection:
            changed = connection.execute("DELETE FROM ui_notes WHERE note_id = ?",
                                         (name,)).rowcount
            connection.commit()
        return bool(changed)
