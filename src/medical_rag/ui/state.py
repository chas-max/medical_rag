"""Durable viewer state and tenant-scoped chat sessions.

Streamlit drops ``session_state`` when the process restarts, so the last viewed
page, the last opened conversation and the tenant/conversation map are written to
the local state database. The chat answers themselves live in the RAG backend's
own history store; this module only keeps the mapping that decides which
conversations a tenant may see in the sidebar.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from .storage import UiSqliteStore, UiStoreError  # noqa: F401 - re-exported for callers

PAGE_KEY = "ui.page"
MAX_ID_CHARS = 128
MAX_TITLE_CHARS = 200
MAX_KEY_CHARS = 200
MAX_VALUE_BYTES = 64 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS ui_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ui_chat_sessions (
    tenant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS ui_chat_sessions_tenant
    ON ui_chat_sessions(tenant_id, updated DESC);
"""


def _require_text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 不能为空")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{name} 超过 {limit} 字符上限")
    return text


def new_conversation_id(tenant_id: str) -> str:
    """Conversation ids are namespaced per tenant so the UI can filter safely.

    The backend treats the id as opaque; the prefix only keeps this viewer's
    conversations identifiable when the legacy backend lists all conversations.
    It is an application-level namespace, not backend authorization.
    """
    tenant = _require_text(tenant_id, "租户", MAX_ID_CHARS)
    return f"{tenant[:12]}-{uuid4().hex}"


class UiStateStore(UiSqliteStore):
    """Key/value view state plus the tenant-to-conversation index."""

    SCHEMA = SCHEMA

    # -- generic view state -------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """Return one stored JSON value, or ``default`` when absent/unreadable."""
        name = _require_text(key, "状态键", MAX_KEY_CHARS)
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM ui_state WHERE key = ?",
                                     (name,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        """Persist one JSON-serialisable value."""
        name = _require_text(key, "状态键", MAX_KEY_CHARS)
        try:
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("界面状态值必须是可序列化的 JSON 数据") from error
        if len(payload.encode("utf-8")) > MAX_VALUE_BYTES:
            raise ValueError(f"界面状态值超过 {MAX_VALUE_BYTES} 字节上限")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO ui_state(key, value, updated) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (name, payload, time.time()),
            )
            connection.commit()

    def delete(self, key: str) -> bool:
        name = _require_text(key, "状态键", MAX_KEY_CHARS)
        with self.connect() as connection:
            changed = connection.execute("DELETE FROM ui_state WHERE key = ?", (name,)).rowcount
            connection.commit()
        return bool(changed)

    def snapshot(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM ui_state ORDER BY key").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
        return result

    # -- page selection -----------------------------------------------------
    def get_page(self, default: str) -> str:
        value = self.get(PAGE_KEY, default)
        return value if isinstance(value, str) and value.strip() else default

    def set_page(self, page: str) -> None:
        self.set(PAGE_KEY, _require_text(page, "页面", MAX_KEY_CHARS))

    # -- tenant-scoped chat sessions ---------------------------------------
    def remember_session(self, tenant_id: str, conversation_id: str, title: str) -> None:
        """Create or refresh the tenant's ownership/title for one conversation."""
        tenant = _require_text(tenant_id, "租户", MAX_ID_CHARS)
        conversation = _require_text(conversation_id, "会话", MAX_ID_CHARS)
        label = (title or "新对话").strip()[:MAX_TITLE_CHARS] or "新对话"
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO ui_chat_sessions(tenant_id, conversation_id, title, created, updated) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(tenant_id, conversation_id) DO UPDATE SET "
                "title=excluded.title, updated=excluded.updated",
                (tenant, conversation, label, now, now),
            )
            connection.commit()

    def forget_session(self, tenant_id: str, conversation_id: str) -> bool:
        with self.connect() as connection:
            changed = connection.execute(
                "DELETE FROM ui_chat_sessions WHERE tenant_id = ? AND conversation_id = ?",
                (_require_text(tenant_id, "租户", MAX_ID_CHARS),
                 _require_text(conversation_id, "会话", MAX_ID_CHARS)),
            ).rowcount
            connection.commit()
        return bool(changed)

    def list_sessions(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Tenant conversations, most recently used first (titles updated locally)."""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT conversation_id, title, created, updated FROM ui_chat_sessions "
                "WHERE tenant_id = ? ORDER BY updated DESC LIMIT ?",
                (_require_text(tenant_id, "租户", MAX_ID_CHARS), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def owned_conversation_ids(self, tenant_id: str) -> set[str]:
        return {row["conversation_id"] for row in self.list_sessions(tenant_id, limit=1000)}

    def conversation_title(self, tenant_id: str, conversation_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT title FROM ui_chat_sessions WHERE tenant_id = ? AND conversation_id = ?",
                (_require_text(tenant_id, "租户", MAX_ID_CHARS),
                 _require_text(conversation_id, "会话", MAX_ID_CHARS)),
            ).fetchone()
        return str(row["title"]) if row else None

    def get_last_conversation(self, tenant_id: str) -> str | None:
        value = self.get(self._last_conversation_key(tenant_id), None)
        return value if isinstance(value, str) and value.strip() else None

    def set_last_conversation(self, tenant_id: str, conversation_id: str) -> None:
        self.set(self._last_conversation_key(tenant_id),
                 _require_text(conversation_id, "会话", MAX_ID_CHARS))

    @staticmethod
    def _last_conversation_key(tenant_id: str) -> str:
        return "ui.conversation." + _require_text(tenant_id, "租户", MAX_ID_CHARS)
