"""Durable local chat history, independent of browser and workflow lifetimes."""

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from time import time_ns
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .settings_store import resolve_path
from .types import ApiError


class ChatMessage(BaseModel):
    """Browser-compatible message; transient streaming flags are not stored."""

    role: Literal["user", "assistant"]
    content: str
    citations: list[dict] | None = None
    analysis: dict | None = None
    abstained: bool | None = None
    abstainReason: str | None = None
    runId: str | None = None
    trace: list[dict] | None = None
    kind: str | None = None
    profile: dict | None = None
    episode: dict | None = None
    error: dict | None = None


class Conversation(BaseModel):
    """A legacy browser conversation accepted for one-time import."""

    id: str = Field(min_length=1, max_length=128)
    title: str
    messages: list[ChatMessage]
    failedRuns: list[dict] = Field(default_factory=list)


class HistoryImport(BaseModel):
    """Import is insert-only: stale browser data cannot replace server answers."""

    conversations: list[Conversation]


class ChatHistoryStore:
    """SQLite transactions keep questions and final answers across restarts."""

    def __init__(self, path: str | Path) -> None:
        self.path = resolve_path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Lazy initialization avoids filesystem writes merely from importing api.py.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=30)) as connection, connection:
                connection.row_factory = sqlite3.Row
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        id TEXT PRIMARY KEY, title TEXT NOT NULL,
                        legacy TEXT NOT NULL DEFAULT '[]',
                        failed_runs TEXT NOT NULL DEFAULT '[]', updated INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS turns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL, question TEXT NOT NULL,
                        answer TEXT, failure TEXT
                    );
                    CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id, id);
                    CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS chat_requests (
                        request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, turn_id INTEGER NOT NULL,
                        run_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL,
                        lease_until REAL NOT NULL, owner_token TEXT,
                        preflight TEXT, result TEXT);
                """)
                yield connection
        except (sqlite3.Error, OSError) as error:
            raise ApiError("HISTORY_STORAGE_ERROR", "历史记录无法读写，请检查存储路径、权限和磁盘空间", 503) from error

    @staticmethod
    def _conversation(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        messages = json.loads(row["legacy"])
        failures = json.loads(row["failed_runs"])
        for turn in connection.execute(
            "SELECT * FROM turns WHERE conversation_id = ? ORDER BY id", (row["id"],)
        ):
            messages.append({"role": "user", "content": turn["question"]})
            if turn["answer"]:
                messages.append(json.loads(turn["answer"]))
            if turn["failure"]:
                failures.append(json.loads(turn["failure"]))
        return {"id": row["id"], "title": row["title"], "messages": messages,
                "failedRuns": failures}

    def list_conversations(self) -> list[dict]:
        """Return all chats, most recently updated first."""
        with self._connect() as connection:
            return [self._conversation(connection, row) for row in connection.execute(
                "SELECT * FROM conversations ORDER BY updated DESC, id")]

    def get_conversation(self, conversation_id: str) -> dict:
        """Return one chat without consulting volatile workflow state."""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM conversations WHERE id = ?",
                                     (conversation_id,)).fetchone()
            if row is None:
                raise ApiError("CONVERSATION_NOT_FOUND", "对话记录不存在", 404)
            return self._conversation(connection, row)

    def import_conversations(self, items: list[Conversation]) -> list[dict]:
        """Import missing legacy IDs only, preserving newer server-side turns."""
        with self._connect() as connection:
            stamp = time_ns()
            for index, item in enumerate(items):
                connection.execute(
                    "INSERT OR IGNORE INTO conversations VALUES (?, ?, ?, ?, ?)",
                    (item.id, item.title,
                     json.dumps([message.model_dump(exclude_none=True) for message in item.messages],
                                ensure_ascii=False),
                     json.dumps(item.failedRuns, ensure_ascii=False), stamp - index))
        return self.list_conversations()

    def begin_turn(self, conversation_id: str, question: object) -> int:
        """Commit a question before starting the potentially long workflow."""
        if not isinstance(question, str) or not question.strip():
            raise ApiError("INVALID_QUERY", "问题不能为空", 422)
        question = question.strip()
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO conversations(id, title, updated) VALUES (?, ?, ?)",
                               (conversation_id, question[:24], time_ns()))
            cursor = connection.execute(
                "INSERT INTO turns(conversation_id, question) VALUES (?, ?)",
                (conversation_id, question))
            connection.execute("UPDATE conversations SET updated = ? WHERE id = ?",
                               (time_ns(), conversation_id))
            return cursor.lastrowid

    def begin_request(self, conversation_id: str, request_id: str, question: str,
                      fingerprint: str, preflight: dict | None = None) -> dict:
        """Claim a durable message lease; retries reuse the same turn and run."""
        if not isinstance(question, str) or not question.strip():
            raise ApiError("INVALID_QUERY", "问题不能为空", 422)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM chat_requests WHERE request_id=?", (request_id,)).fetchone()
            if row and (row["fingerprint"] != fingerprint or row["conversation_id"] != conversation_id):
                raise ApiError("REQUEST_ID_CONFLICT", "请求标识已用于不同内容", 409)
            if row and row["state"] == "completed":
                return {**dict(row), "result": json.loads(row["result"])}
            if row and row["lease_until"] > time.time():
                raise ApiError("REQUEST_IN_PROGRESS", "相同请求仍在处理中，请稍后重试", 409)
            token = uuid4().hex
            if row:
                connection.execute("UPDATE chat_requests SET owner_token=?,lease_until=? WHERE request_id=?",
                                   (token, time.time() + 30, request_id))
                return {**dict(row), "state": "recoverable", "owner_token": token}
            connection.execute("INSERT OR IGNORE INTO conversations(id,title,updated) VALUES (?,?,?)",
                               (conversation_id, question[:24], time_ns()))
            cursor = connection.execute("INSERT INTO turns(conversation_id,question) VALUES (?,?)",
                                        (conversation_id, question))
            run_id = uuid4().hex
            initial = json.dumps(preflight, ensure_ascii=False) if preflight is not None else None
            connection.execute("INSERT INTO chat_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (request_id, conversation_id, fingerprint, cursor.lastrowid, run_id,
                                "processing", time.time() + 30, token, initial, None))
            return {"turn_id": cursor.lastrowid, "run_id": run_id, "state": "new",
                    "owner_token": token, "preflight": initial}

    def renew_request(self, request_id: str, owner_token: str) -> bool:
        """Fence a live owner without changing an already completed request."""
        with self._connect() as connection:
            return bool(connection.execute("UPDATE chat_requests SET lease_until=? "
                "WHERE request_id=? AND owner_token=? AND state='processing'",
                (time.time() + 30, request_id, owner_token)).rowcount)

    def save_preflight(self, request_id: str, owner_token: str, preflight: dict) -> None:
        """Save only routing metadata; profile facts remain in their own store."""
        with self._connect() as connection:
            changed = connection.execute("UPDATE chat_requests SET preflight=? "
                "WHERE request_id=? AND owner_token=? AND state='processing'",
                (json.dumps(preflight, ensure_ascii=False), request_id, owner_token)).rowcount
            if not changed:
                raise ApiError("REQUEST_LEASE_LOST", "请求已由其他进程接管，请重试读取结果", 409)

    def finish_turn(self, conversation_id: str | None, turn_id: int | None, result: dict,
                    on_saved: Callable[[sqlite3.Connection], None] | None = None,
                    request_id: str | None = None, owner_token: str | None = None) -> None:
        """Commit the authoritative result before HTTP/SSE announces completion."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if request_id:
                row = connection.execute("SELECT owner_token,state FROM chat_requests WHERE request_id=?",
                                         (request_id,)).fetchone()
                if not row or row["owner_token"] != owner_token or row["state"] != "processing":
                    raise ApiError("REQUEST_LEASE_LOST", "请求已由其他进程接管，请重试读取结果", 409)
            connection.execute("INSERT OR REPLACE INTO runs VALUES (?, ?)",
                               (result["run_id"], json.dumps(result, ensure_ascii=False)))
            if conversation_id is None or turn_id is None:
                return
            output = result["output"]
            answer = failure = None
            if (result["status"] == "success" and (output.get("answer", "").strip()
                                                   or output.get("abstained"))) or result.get("profile", {}).get("actions") or result.get("error"):
                payload = {
                    "role": "assistant", "content": output.get("answer") or "",
                    "citations": output.get("citations") or [], "analysis": output.get("analysis"),
                    "abstained": output.get("abstained", False),
                    "runId": result["run_id"], "trace": result["trace"],
                    **{key: result[key] for key in ("kind", "profile", "episode", "error") if key in result},
                }
                # 拒答原因随历史保存：否则重新打开会话时，生成故障又会被界面显示成“证据不足”。
                if output.get("abstain_reason"):
                    payload["abstainReason"] = output["abstain_reason"]
                answer = json.dumps(payload, ensure_ascii=False)
            if result["status"] != "success" or answer is None:
                failure = json.dumps({"runId": result["run_id"], "trace": result["trace"]},
                                     ensure_ascii=False)
            connection.execute("UPDATE turns SET answer = ?, failure = ? WHERE id = ? AND conversation_id = ?",
                               (answer, failure, turn_id, conversation_id))
            connection.execute("UPDATE conversations SET updated = ? WHERE id = ?",
                               (time_ns(), conversation_id))
            if answer and on_saved is not None:
                # The outbox is atomic with the final answer when healthy. A memory
                # dependency failure rolls back only its savepoint, never the answer.
                connection.execute("SAVEPOINT memory_outbox")
                try:
                    on_saved(connection)
                except Exception:  # noqa: BLE001 - memory must not roll back an authoritative answer
                    connection.execute("ROLLBACK TO memory_outbox")
                    result["memory"] = {"status": "failed"}
                    if "episode" in result:
                        result["episode"] = {"status": "failed"}
                finally:
                    connection.execute("RELEASE memory_outbox")
                connection.execute("UPDATE runs SET body=? WHERE id=?",
                                   (json.dumps(result, ensure_ascii=False), result["run_id"]))
                if answer:
                    updated_answer = json.loads(answer)
                    for key in ("episode", "memory"):
                        if key in result:
                            updated_answer[key] = result[key]
                    connection.execute("UPDATE turns SET answer=? WHERE id=?",
                                       (json.dumps(updated_answer, ensure_ascii=False), turn_id))
            if request_id:
                connection.execute("UPDATE chat_requests SET state='completed',result=?,lease_until=0 WHERE request_id=?",
                                   (json.dumps(result, ensure_ascii=False), request_id))

    def resolve_confirmation(self, token: str, outcome: dict) -> None:
        """Refresh the original notice after explicit confirmation or cancellation."""
        with self._connect() as connection:
            for row in connection.execute("SELECT id,answer FROM turns WHERE answer IS NOT NULL").fetchall():
                answer = json.loads(row["answer"])
                if answer.get("profile", {}).get("confirmation_token") == token:
                    answer["profile"] = outcome
                    connection.execute("UPDATE turns SET answer=? WHERE id=?",
                                       (json.dumps(answer, ensure_ascii=False), row["id"]))
            for row in connection.execute("SELECT id,body FROM runs").fetchall():
                result = json.loads(row["body"])
                if result.get("profile", {}).get("confirmation_token") == token:
                    result["profile"] = outcome
                    encoded = json.dumps(result, ensure_ascii=False)
                    connection.execute("UPDATE runs SET body=? WHERE id=?", (encoded, row["id"]))
                    connection.execute("UPDATE chat_requests SET result=? WHERE run_id=? AND state='completed'",
                                       (encoded, row["id"]))

    def release_request(self, request_id: str, owner_token: str) -> None:
        """Permit an explicit retry after a rejected confirmation, without a new turn."""
        with self._connect() as connection:
            connection.execute("UPDATE chat_requests SET lease_until=0 WHERE request_id=? AND owner_token=? AND state='processing'",
                               (request_id, owner_token))

    def get_run(self, run_id: str) -> dict | None:
        """Read an HTTP workflow result after its in-memory cache has gone away."""
        with self._connect() as connection:
            row = connection.execute("SELECT body FROM runs WHERE id = ?", (run_id,)).fetchone()
            return json.loads(row["body"]) if row else None
