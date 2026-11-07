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

from .dialogue_models import ConversationState, DialogueSnapshot, RecentTurn, Topic
from .dialogue_state import (
    STATE_SCHEMA,
    advance_state,
    bump_sqlite_context,
    can_commit_semantics,
    decode_state,
    invalidate_sqlite_state,
    write_sqlite_state,
)
from .settings_store import resolve_path
from .short_term import MAX_SUMMARY_CHARS, WINDOW_TURNS, recent_window
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
                    -- Rolling digest of the turns that already fell out of the short-term
                    -- window. It is derived data: deleting a conversation or its turns
                    -- must delete the row too, so history stays the single source of truth.
                    CREATE TABLE IF NOT EXISTS conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        through_turn INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        updated INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS chat_requests (
                        request_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, turn_id INTEGER NOT NULL,
                        run_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL,
                        lease_until REAL NOT NULL, owner_token TEXT,
                        preflight TEXT, result TEXT);
                """ + STATE_SCHEMA)
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

    # -- short-term memory: recent window plus a rolling digest ---------------
    def answered_through(self, conversation_id: str) -> int:
        """Highest answered turn id, or 0 when the conversation has no answer yet."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(id) AS last FROM turns WHERE conversation_id=? AND answer IS NOT NULL",
                (conversation_id,)).fetchone()
        return int(row["last"] or 0) if row else 0

    def recent_turns(self, conversation_id: str, limit: int = 3) -> list[dict]:
        """Newest answered turns in chronological order, with plain answer text."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, question, answer FROM turns "
                "WHERE conversation_id=? AND answer IS NOT NULL ORDER BY id DESC LIMIT ?",
                (conversation_id, limit)).fetchall()
        turns = []
        for row in reversed(rows):
            answer = self._answer_text(row["answer"])
            if answer is None:
                continue
            turns.append({"turn": int(row["id"]), "question": row["question"], "answer": answer})
        return turns

    def turns_between(self, conversation_id: str, after: int, through: int) -> list[dict]:
        """Answered turns in ``(after, through]``: exactly what a digest may absorb."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, question, answer FROM turns WHERE conversation_id=? AND id>? AND id<=? "
                "AND answer IS NOT NULL ORDER BY id", (conversation_id, after, through)).fetchall()
        turns = []
        for row in rows:
            answer = self._answer_text(row["answer"])
            if answer is not None:
                turns.append({"turn": int(row["id"]), "question": row["question"], "answer": answer})
        return turns

    def summary(self, conversation_id: str) -> tuple[int, str]:
        """The persisted digest and the turn it already covers (0 when absent)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT through_turn, text FROM conversation_summaries WHERE conversation_id=?",
                (conversation_id,)).fetchone()
        return (int(row["through_turn"]), row["text"]) if row else (0, "")

    def save_summary(self, conversation_id: str, through_turn: int, text: str) -> None:
        """Replace the digest; callers must pass an already bounded summary."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversation_summaries VALUES (?,?,?,?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET through_turn=excluded.through_turn, "
                "text=excluded.text, updated=excluded.updated",
                (conversation_id, through_turn, text, time_ns()))

    def digest_candidates(self, conversation_id: str, covered: int,
                          window_turns: int = WINDOW_TURNS) -> list[dict]:
        """Answered turns that sit before the window and after the digest.

        Turn ids are global and may have gaps, so the boundary is computed from
        this conversation's own ordered rows, never from id arithmetic.

        Args:
            conversation_id: Conversation to read.
            covered: Highest turn id already absorbed by the digest.
            window_turns: Number of newest answered turns the window still shows.

        Returns:
            Chronological unanswered-window candidates with plain answer text.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,question,answer FROM turns WHERE conversation_id=? AND answer IS NOT NULL ORDER BY id",
                (conversation_id,),
            ).fetchall()
        completed = [row for row in rows if self._answer_text(row["answer"]) is not None]
        outside = completed[:-window_turns] if window_turns > 0 else completed
        return [{"turn": int(row["id"]), "question": row["question"],
                 "answer": self._answer_text(row["answer"])}
                for row in outside if int(row["id"]) > covered]

    def dialogue_snapshot(self, conversation_id: str) -> DialogueSnapshot:
        """Read window, digest and structured state in one SQLite read transaction.

        A single transaction is required: reading the three parts through separate
        connections could stitch together versions that never coexisted, which is
        exactly how a follow-up ends up pointing at a topic the window lost.

        Args:
            conversation_id: Conversation to read.

        Returns:
            A bounded snapshot. An invalid or stale persisted state degrades to
            ``None`` while the authorised recent window is still returned.
        """
        with self._connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT id, question, answer FROM turns WHERE conversation_id=? "
                "AND answer IS NOT NULL ORDER BY id DESC", (conversation_id,)).fetchall()
            completed = []
            kinds: dict[int, str] = {}
            for row in rows:
                text = self._answer_text(row["answer"])
                if text is None:
                    continue
                turn = int(row["id"])
                kinds[turn] = self._answer_type(row["answer"])
                completed.append({"turn": turn, "question": row["question"], "answer": text})
            window = recent_window(completed, WINDOW_TURNS)
            summary_row = connection.execute(
                "SELECT through_turn, text FROM conversation_summaries WHERE conversation_id=?",
                (conversation_id,)).fetchone()
            state_row = connection.execute(
                "SELECT state_json, context_version FROM conversation_states WHERE conversation_id=?",
                (conversation_id,)).fetchone()
            context_version = int(state_row["context_version"]) if state_row else 0
            state = decode_state(state_row["state_json"]) if state_row else None
            if state is not None and not self._state_sourced(
                    connection, conversation_id, state):
                state = None
        recent = tuple(
            RecentTurn(turn_id=str(item["turn"]), user=item["user"],
                       assistant=item["assistant"], answer_type=kinds[item["turn"]])
            for item in window)
        summary = (summary_row["text"][:MAX_SUMMARY_CHARS] if summary_row else "")
        return DialogueSnapshot(recent=recent, summary=summary, state=state,
                                context_version=context_version)

    def _state_sourced(self, connection: sqlite3.Connection, conversation_id: str,
                       state: ConversationState) -> bool:
        """Re-prove every persisted topic against this conversation's own turns."""
        topics: list[Topic] = []
        if state.active_topic is not None:
            topics.append(state.active_topic)
        if state.pending_clarification is not None:
            topics.extend(state.pending_clarification.candidates)
        for topic in topics:
            try:
                turn = int(topic.source_turn_id)
            except (TypeError, ValueError):
                return False
            row = connection.execute(
                "SELECT question, answer FROM turns WHERE id=? AND conversation_id=?",
                (turn, conversation_id)).fetchone()
            if row is None:
                return False
            text = row["question"] if topic.source_role == "user" else (
                self._answer_text(row["answer"]) or "")
            if topic.name not in text:
                return False
        return True

    @staticmethod
    def _answer_text(raw: str) -> str | None:
        """Plain prose of one stored answer, or ``None`` when the blob is unusable."""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        content = payload.get("content")
        return content if isinstance(content, str) and content.strip() else None

    @staticmethod
    def _answer_type(raw: str) -> str:
        """Stored answer kind; legacy blobs without one read as a normal answer."""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return "answer"
        if isinstance(payload, dict) and payload.get("answerType") == "clarification":
            return "clarification"
        return "answer"

    def delete_conversation(self, conversation_id: str) -> list[str] | None:
        """Delete the conversation and everything derived from it, atomically.

        Returns the run ids that belonged to it (so the caller can delete episodes),
        or ``None`` when the conversation does not exist. A live request blocks the
        delete: removing turns under a running answer would lose that answer.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT id FROM conversations WHERE id=?",
                                     (conversation_id,)).fetchone()
            if row is None:
                return None
            busy = connection.execute(
                "SELECT 1 FROM chat_requests WHERE conversation_id=? AND state='processing' "
                "AND lease_until>?", (conversation_id, time.time())).fetchone()
            if busy is not None:
                raise ApiError("REQUEST_IN_PROGRESS", "该对话仍有回答在处理中，请稍后再删除", 409)
            run_ids = [item["run_id"] for item in connection.execute(
                "SELECT run_id FROM chat_requests WHERE conversation_id=?", (conversation_id,))]
            connection.execute("DELETE FROM turns WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM conversation_summaries WHERE conversation_id=?",
                               (conversation_id,))
            # Derived semantic state is removed with its source rows in this same
            # transaction; there is no foreign key to rely on.
            connection.execute("DELETE FROM conversation_states WHERE conversation_id=?",
                               (conversation_id,))
            connection.execute("DELETE FROM chat_requests WHERE conversation_id=?",
                               (conversation_id,))
            connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            for run_id in run_ids:
                connection.execute("DELETE FROM runs WHERE id=?", (run_id,))
            return run_ids

    def import_conversations(self, items: list[Conversation]) -> list[dict]:
        """Import missing legacy IDs only, preserving newer server-side turns."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stamp = time_ns()
            for index, item in enumerate(items):
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO conversations VALUES (?, ?, ?, ?, ?)",
                    (item.id, item.title,
                     json.dumps([message.model_dump(exclude_none=True) for message in item.messages],
                                ensure_ascii=False),
                     json.dumps(item.failedRuns, ensure_ascii=False), stamp - index)).rowcount
                if inserted:
                    # Imported legacy text changes what the conversation contains,
                    # so any snapshot taken before it must be detected as stale.
                    bump_sqlite_context(connection, item.id)
        return self.list_conversations()

    def begin_turn(self, conversation_id: str, question: object) -> int:
        """Commit a question before starting the potentially long workflow."""
        if not isinstance(question, str) or not question.strip():
            raise ApiError("INVALID_QUERY", "问题不能为空", 422)
        question = question.strip()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR IGNORE INTO conversations(id, title, updated) VALUES (?, ?, ?)",
                               (conversation_id, question[:24], time_ns()))
            cursor = connection.execute(
                "INSERT INTO turns(conversation_id, question) VALUES (?, ?)",
                (conversation_id, question))
            connection.execute("UPDATE conversations SET updated = ? WHERE id = ?",
                               (time_ns(), conversation_id))
            bump_sqlite_context(connection, conversation_id)
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
            busy = connection.execute(
                "SELECT 1 FROM chat_requests WHERE conversation_id=? AND request_id<>? "
                "AND state='processing' AND lease_until>? LIMIT 1",
                (conversation_id, request_id, time.time()),
            ).fetchone()
            if busy:
                raise ApiError("REQUEST_IN_PROGRESS", "该会话仍有问题正在处理中", 409)
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
            # The pending question is a content change: a snapshot read before it
            # must not be allowed to commit on top of the new state.
            bump_sqlite_context(connection, conversation_id)
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
                    request_id: str | None = None, owner_token: str | None = None,
                    *, dialogue: object | None = None,
                    invalidate_state: bool = False) -> None:
        """Commit the authoritative result before HTTP/SSE announces completion.

        When ``dialogue`` carries a validated resolution, the reduced state commits
        in this same transaction under the snapshot's content version. A conflict or
        an oversized state rolls back the answer too: a stored answer whose semantic
        state was never committed would mislead the next turn.

        ``invalidate_state`` marks the derived state for rebuild: a turn answered
        while the dialogue switch was off has semantics nobody computed.
        """
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
            expected = self._dialogue_version(connection, conversation_id, dialogue)
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
                if output.get("answer_type"):
                    payload["answerType"] = output["answer_type"]
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
            self._commit_dialogue(connection, conversation_id, turn_id, result, dialogue,
                                  expected, invalidate_state)
            if request_id:
                connection.execute("UPDATE chat_requests SET state='completed',result=?,lease_until=0 WHERE request_id=?",
                                   (json.dumps(result, ensure_ascii=False), request_id))

    @staticmethod
    def _dialogue_version(connection: sqlite3.Connection, conversation_id: str,
                          dialogue: object | None) -> int:
        """The content version a dialogue snapshot must still match at commit."""
        if dialogue is None:
            return 0
        snapshot = getattr(dialogue, "snapshot", None)
        expected = int(getattr(snapshot, "context_version", 0) or 0)
        row = connection.execute(
            "SELECT context_version FROM conversation_states WHERE conversation_id=?",
            (conversation_id,)).fetchone()
        stored = int(row["context_version"]) if row else 0
        if stored != expected:
            raise ApiError("CONVERSATION_CONTEXT_CHANGED", "会话已更新，请重新发送问题", 409)
        return expected

    def _commit_dialogue(self, connection: sqlite3.Connection, conversation_id: str,
                         turn_id: int, result: dict, dialogue: object | None,
                         expected: int, invalidate: bool) -> None:
        """Persist the reduced state, or advance the version for non-semantic turns."""
        resolution = getattr(dialogue, "resolution", None) if dialogue is not None else None
        snapshot = getattr(dialogue, "snapshot", None) if dialogue is not None else None
        if resolution is not None and snapshot is not None and can_commit_semantics(
                str(result.get("status")), result.get("output") or {}):
            state = advance_state(getattr(snapshot, "state", None), resolution,
                                  turn_id=str(turn_id))
            write_sqlite_state(connection, conversation_id, state, expected)
            dialogue.snapshot = DialogueSnapshot(
                state=state, recent=snapshot.recent, summary=snapshot.summary,
                context_version=expected + 1)
            return
        if invalidate:
            # Semantics were never computed for this answer, so the derived state
            # cannot be trusted on the next enabled turn.
            invalidate_sqlite_state(connection, conversation_id)
            return
        # A technical failure or a plain content write still changes the stored
        # conversation, so the version moves without touching an already valid topic.
        bump_sqlite_context(connection, conversation_id)

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
