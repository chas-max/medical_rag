"""Single-user long-term memory: relational profiles and authoritative Markdown episodes."""

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .jobs import enqueue_task, initialize_outbox
from .settings_store import resolve_path
from .types import ApiError

USER = "local-user"
PROFILE_KEYS = {"display_name": "background", "occupation": "background",
                "language": "preferences", "response_style": "preferences",
                "interests": "interests", "goals": "interests", "health": "health"}
SAFE_PREFERENCES = {
    "language": {"中文", "简体中文", "繁体中文", "英文", "英语", "Chinese", "English"},
    "response_style": {"简短", "简洁", "详细", "分点", "通俗", "专业", "列表", "简短中文", "详细中文",
                       "分点中文", "简洁中文", "通俗中文", "简短英文", "详细英文"},
}
PUBLIC_TOPICS = {"高血压", "糖尿病", "哮喘", "随访", "复诊", "血压测量", "循证医学", "医学检索", "健康科普"}
RECALL_STOP_WORDS = {"如何", "怎么", "什么", "问题", "解决", "一般", "知识", "讨论", "公开", "资料",
                    "用户", "本人", "病史", "不是", "代表", "医学", "证据", "明确", "提供", "回答", "偏好"}
SELF_REPORT_PATTERNS = {
    "display_name": r"(?:我叫|我的名字是|请叫我|My name is |Call me )",
    "occupation": r"(?:我是(?:一名|一个)?|我的职业是|I am (?:a |an )?|I work as (?:a |an )?)",
    "language": r"(?:我(?:喜欢|偏好|希望)|请用|回答请用|I (?:prefer|like|want) )",
    "response_style": r"(?:我(?:喜欢|偏好|希望)|请用|回答请用|I (?:prefer|like|want) )",
    "interests": r"(?:我(?:喜欢|关注|感兴趣的是)|我的兴趣是|I (?:like|am interested in) )",
    "goals": r"(?:我的目标是|我希望|I (?:aim to|want to) )",
    "health": r"(?:我(?:患有|有|确诊为|被确诊为|正在服用|服用)|I (?:have|take|am taking|was diagnosed with|am diagnosed with|am allergic to) )",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_settings (
    user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, revision INTEGER NOT NULL);
INSERT OR IGNORE INTO memory_settings VALUES ('local-user', 1, 0);
CREATE TABLE IF NOT EXISTS memory_profiles (
    key TEXT PRIMARY KEY, user_id TEXT NOT NULL, category TEXT NOT NULL,
    value TEXT NOT NULL, sensitive INTEGER NOT NULL, source TEXT NOT NULL,
    support TEXT NOT NULL, source_conversation_id TEXT, source_run_id TEXT,
    source_order INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_episodes (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL,
    tags TEXT NOT NULL, created_at TEXT NOT NULL, source_conversation_id TEXT NOT NULL,
    source_run_id TEXT UNIQUE NOT NULL, sensitive INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_jobs (
    run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
    source_order INTEGER NOT NULL, question TEXT NOT NULL, output TEXT NOT NULL,
    revision INTEGER NOT NULL, consent INTEGER NOT NULL, status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, available REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0, token TEXT, error TEXT,
    created_at TEXT NOT NULL, contract_version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS memory_migrations (
    name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS profile_versions (
    user_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, last_source_order INTEGER NOT NULL);
INSERT OR IGNORE INTO profile_versions VALUES ('local-user',0,0);
CREATE TABLE IF NOT EXISTS profile_requests (
    request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    outcome TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS profile_confirmations (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, request_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL, profile_revision INTEGER NOT NULL,
    privacy_revision INTEGER NOT NULL, mutations TEXT NOT NULL,
    requires_consent INTEGER NOT NULL, expires_at REAL NOT NULL,
    status TEXT NOT NULL, outcome TEXT);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sensitive_text(text: str) -> bool:
    """Conservative supplement to model labels; uncertainty must not grant consent."""
    contact = re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|\d[\d -]{6,}\d", text)
    health = re.search(
        r"病史|症状|用药|服用|服药|患有|确诊|诊断|检查结果|过敏|疼|痛|血压|血糖|失眠|怀孕|抑郁|焦虑|药物|手术|发烧|发热|咳嗽|糖尿病|癌|肿瘤|阿司匹林", text)
    personal = re.search(r"我|本人|家人|父亲|母亲|爸爸|妈妈|孩子|妻子|丈夫|患者\d|\bI\b|\bmy\b", text, re.IGNORECASE)
    return bool(contact or (personal and health))


def episode_text_sensitive(episode: dict) -> bool:
    """Scan every stored episode field; the database gate, not a model label."""
    fields = [episode.get("title", ""), episode.get("summary", "")]
    for key in ("events", "decisions", "unresolved", "followups", "tags"):
        value = episode.get(key, [])
        fields.extend(value if isinstance(value, list) else [str(value)])
    return any(sensitive_text(text) for text in fields if isinstance(text, str))


def _text(value: object, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid bounded memory text")
    return value.strip()


def _profile(key: str, value: str, category: str, sensitive: bool) -> None:
    if (PROFILE_KEYS.get(key) != category or len(key) > 128 or
            not isinstance(sensitive, bool)):
        raise ValueError("Invalid profile key or category")
    _text(value)
    # Manually writing a health field is an explicit, separately labelled consent.
    if (key == "health" or sensitive_text(value)) and not sensitive:
        raise ValueError("Sensitive memories require explicit consent")


class MemoryStore:
    """All mutations serialize on SQLite, including file writes and forgetting."""

    def __init__(self, db_path: str | Path, episodes_dir: str | Path) -> None:
        self.path = resolve_path(db_path)
        self.root = resolve_path(episodes_dir).resolve()

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=30)) as connection, connection:
                connection.row_factory = sqlite3.Row
                connection.executescript(SCHEMA)
                # Serialize incremental upgrades across new service connections.
                connection.execute("BEGIN IMMEDIATE")
                self._migrate_episode_only(connection)
                self._initialize_dispatch(connection)
                connection.commit()
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        except (OSError, sqlite3.Error) as error:
            raise ApiError("MEMORY_STORAGE_ERROR", "长期记忆存储不可用，请检查路径、权限和磁盘空间", 503) from error

    @staticmethod
    def _initialize_dispatch(connection: sqlite3.Connection) -> None:
        initialize_outbox(connection)
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(memory_jobs)')}
        if 'dispatch_seq' not in columns:
            connection.execute('ALTER TABLE memory_jobs ADD COLUMN dispatch_seq INTEGER NOT NULL DEFAULT 0')
        # Idempotent backfill never resets attempts or resurrects privacy-invalid work.
        rows = connection.execute("SELECT j.* FROM memory_jobs j JOIN memory_settings s ON j.user_id=s.user_id "
                                  "WHERE j.status IN ('pending','processing') AND s.enabled=1 AND j.revision=s.revision").fetchall()
        for row in rows:
            enqueue_task(connection, 'memory.extract', row['run_id'], row['dispatch_seq'], row['available'])

    @staticmethod
    def _migrate_episode_only(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(memory_jobs)")}
        if "contract_version" not in columns:
            connection.execute("ALTER TABLE memory_jobs ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 1")
        migrated = connection.execute(
            "SELECT 1 FROM memory_migrations WHERE name='episode_only_v2'").fetchone()
        if migrated:
            return
        # Revoke legacy leases exactly once. Never reactivate cancelled, cleared,
        # disabled or privacy-version-invalidated work during an upgrade.
        connection.execute(
            "UPDATE memory_jobs SET status='pending',token=NULL,lease_until=0 WHERE status='processing' "
            "AND EXISTS (SELECT 1 FROM memory_settings s WHERE s.user_id=memory_jobs.user_id "
            "AND s.enabled=1 AND s.revision=memory_jobs.revision)")
        connection.execute(
            "UPDATE memory_jobs SET status='cancelled',token=NULL,lease_until=0,question='',output='{}' "
            "WHERE status IN ('pending','processing') AND NOT EXISTS "
            "(SELECT 1 FROM memory_settings s WHERE s.user_id=memory_jobs.user_id "
            "AND s.enabled=1 AND s.revision=memory_jobs.revision)")
        connection.execute("INSERT INTO memory_migrations VALUES ('episode_only_v2')")

    @staticmethod
    def _settings(connection: sqlite3.Connection) -> sqlite3.Row:
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute("SELECT * FROM memory_settings WHERE user_id=?", (USER,)).fetchone()

    @staticmethod
    def _bump(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE memory_settings SET revision=revision+1 WHERE user_id=?", (USER,))
        connection.execute("UPDATE memory_jobs SET status='cancelled', token=NULL, question='', output='{}' "
                           "WHERE status IN ('pending','processing') AND user_id=?", (USER,))
        connection.execute("UPDATE profile_confirmations SET status='cancelled',mutations='[]' "
                           "WHERE status='pending' AND user_id=?", (USER,))

    @staticmethod
    def _profile_changed(connection: sqlite3.Connection, source_order: int = 0) -> None:
        connection.execute("UPDATE profile_versions SET revision=revision+1,"
                           "last_source_order=MAX(last_source_order,?) WHERE user_id=?", (source_order, USER))
        connection.execute("UPDATE profile_confirmations SET status='cancelled',mutations='[]' "
                           "WHERE status='pending' AND user_id=?", (USER,))

    @staticmethod
    def _public_profile(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["value"] = json.loads(value["value"])
        value["sensitive"] = bool(value["sensitive"])
        return value

    def state(self) -> dict:
        """Return management metadata, never a model key or queued raw text."""
        with self._connect() as connection:
            settings = self._settings(connection)
            profiles = [self._public_profile(row) for row in connection.execute(
                "SELECT * FROM memory_profiles WHERE user_id=? ORDER BY key", (USER,))]
            episodes = [dict(row) for row in connection.execute(
                "SELECT id,title,created_at,source_run_id,sensitive,status FROM memory_episodes "
                "WHERE user_id=? AND status='ready' ORDER BY created_at DESC", (USER,))]
            jobs = [dict(row) for row in connection.execute(
                "SELECT run_id,status,error FROM memory_jobs WHERE user_id=? "
                "ORDER BY created_at DESC LIMIT 50", (USER,))]
            return {"enabled": bool(settings["enabled"]), "revision": settings["revision"],
                    "profiles": profiles, "episodes": episodes, "jobs": jobs}

    def job_status(self, run_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT run_id,status,error FROM memory_jobs WHERE run_id=? AND user_id=?",
                                     (run_id, USER)).fetchone()
            return dict(row) if row else {"run_id": run_id, "status": "skipped", "error": None}

    def set_enabled(self, enabled: bool) -> None:
        with self._connect(True) as connection:
            self._bump(connection)
            connection.execute("UPDATE memory_settings SET enabled=? WHERE user_id=?", (enabled, USER))

    def save_profile(self, key: str, value: str, category: str, sensitive: bool = False) -> None:
        """An explicit manual correction takes priority over subsequent extraction."""
        try:
            _profile(key, value, category, sensitive)
        except ValueError as error:
            raise ApiError("INVALID_MEMORY", "画像字段、长度或敏感信息授权不符合要求", 422) from error
        with self._connect(True) as connection:
            self._profile_changed(connection)
            self._upsert_profile(connection, key, value, category, sensitive, "manual", "", None, None, 0)

    @staticmethod
    def _upsert_profile(connection: sqlite3.Connection, key: str, value: str, category: str,
                        sensitive: bool, source: str, support: str, conversation: str | None,
                        run: str | None, order: int) -> None:
        stamp = _now()
        connection.execute(
            "INSERT INTO memory_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "category=excluded.category,value=excluded.value,sensitive=excluded.sensitive,source=excluded.source,"
            "support=excluded.support,source_conversation_id=excluded.source_conversation_id,"
            "source_run_id=excluded.source_run_id,source_order=excluded.source_order,updated_at=excluded.updated_at",
            (key, USER, category, json.dumps(value, ensure_ascii=False), sensitive, source, support,
             conversation, run, order, stamp, stamp))

    def delete_profile(self, key: str) -> None:
        with self._connect(True) as connection:
            self._bump(connection)
            self._profile_changed(connection)
            connection.execute("DELETE FROM memory_profiles WHERE user_id=? AND key=?", (USER, key))

    def _file(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root or path.suffix != ".md":
            raise ApiError("MEMORY_PATH_ERROR", "情景文件路径不安全", 503)
        return path

    def _read(self, row: sqlite3.Row) -> str:
        try:
            path = self._file(row["path"])
            with path.open("rb") as stream:
                data = stream.read(32769)
            if len(data) > 32768:
                raise ValueError("Episode too large")
            content = data.decode("utf-8")
            prefix = "<!-- memory-v1\n"
            if not content.startswith(prefix) or "\n-->\n" not in content:
                raise ValueError("Invalid memory metadata")
            metadata = json.loads(content[len(prefix):].split("\n-->\n", 1)[0])
            if (not isinstance(metadata, dict) or metadata.get("version") != 1 or
                    metadata.get("user_id") != USER or metadata.get("id") != row["id"] or
                    metadata.get("source_run_id") != row["source_run_id"]):
                raise ValueError("Mismatched episode metadata")
            return content
        except (OSError, UnicodeError, ValueError) as error:
            raise ApiError("MEMORY_FILE_ERROR", "情景文件损坏、过大或无法读取", 503) from error

    def read_episode(self, episode_id: str) -> dict:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_episodes WHERE id=? AND user_id=? AND status='ready'",
                                     (episode_id, USER)).fetchone()
            if row is None:
                raise ApiError("MEMORY_NOT_FOUND", "情景记忆不存在或未保存完成", 404)
            return {"id": episode_id, "content": self._read(row)}

    def delete_episode(self, episode_id: str) -> None:
        with self._connect(True) as connection:
            self._bump(connection)
            row = connection.execute("SELECT * FROM memory_episodes WHERE id=? AND user_id=?",
                                     (episode_id, USER)).fetchone()
            if row:
                self._remove_files(row["path"])
                connection.execute("DELETE FROM memory_episodes WHERE id=?", (episode_id,))

    def clear(self) -> None:
        """Delete actual episode bodies before reporting successful forgetting."""
        with self._connect(True) as connection:
            self._bump(connection)
            self._profile_changed(connection)
            for row in connection.execute("SELECT path FROM memory_episodes WHERE user_id=?", (USER,)):
                self._remove_files(row["path"])
            connection.execute("DELETE FROM memory_episodes WHERE user_id=?", (USER,))
            connection.execute("DELETE FROM memory_profiles WHERE user_id=?", (USER,))
            # Clear pending raw extraction material too; original chat history is separate.
            connection.execute("UPDATE memory_jobs SET question='',output='{}' WHERE user_id=?", (USER,))

    def snapshot(self, question: str) -> tuple[int, str]:
        """Return one bounded immutable context, relevant episodes only."""
        with self._connect() as connection:
            settings = self._settings(connection)
            revision = settings["revision"]
            if not settings["enabled"]:
                return revision, ""
            profile_text = "\n".join(
                f"{row['key']}={row['value']}" + ("（敏感自述，未经核实）" if row["sensitive"] else "")
                for row in connection.execute("SELECT key,value,sensitive FROM memory_profiles WHERE user_id=? ORDER BY key LIMIT 64", (USER,)))[:2000]
            terms = set(re.findall(r"[a-zA-Z0-9]{2,}|[\u4e00-\u9fff]{2,}", question.lower()))
            for word in list(terms):
                if re.search(r"[\u4e00-\u9fff]", word):
                    terms.update(word[i:i + 2] for i in range(len(word) - 1))
            terms.difference_update(RECALL_STOP_WORDS)
            scored = []
            for row in connection.execute("SELECT * FROM memory_episodes WHERE user_id=? AND status='ready' ORDER BY created_at DESC LIMIT 200", (USER,)):
                try:
                    content = self._read(row)
                except ApiError:
                    continue
                body = content.split("\n-->\n", 1)[1]
                # File-format headings and stock phrases are not topical evidence.
                searchable = (row["title"] + row["tags"] + re.sub(r"(?m)^#{1,6} .*$", "", body)).lower()
                score = sum(term in searchable for term in terms)
                if score:
                    scored.append((score, row["created_at"], body[:1000]))
            episodes = [item[2] for item in sorted(scored, reverse=True)[:3]]
            parts = (["用户画像（用户提供，非医学证据）：\n" + profile_text] if profile_text else [])
            parts.extend("历史情景（可能过时，非医学证据）：\n" + text for text in episodes)
            return revision, "\n\n".join(parts)[:5000]

    def enqueue(self, connection: sqlite3.Connection, run_id: str, conversation_id: str,
                turn_id: int, question: str, output: dict, revision: int, consent: bool) -> None:
        """Register an outbox item inside the authoritative history transaction."""
        settings = self._settings(connection)
        if not settings["enabled"] or settings["revision"] != revision:
            return
        connection.execute(
            "INSERT OR IGNORE INTO memory_jobs(run_id,user_id,conversation_id,source_order,question,output,"
            "revision,consent,status,created_at,contract_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, USER, conversation_id, turn_id, question, json.dumps(output, ensure_ascii=False),
             revision, consent, "pending", _now(), 2))
        row = connection.execute('SELECT status,dispatch_seq FROM memory_jobs WHERE run_id=?', (run_id,)).fetchone()
        if row and row[0] in ('pending', 'processing'):
            enqueue_task(connection, 'memory.extract', run_id, row[1])

    def claim(self, run_id: str | None = None, dispatch_seq: int | None = None) -> dict | None:
        """Lease at most one ready task. An expired lease can be safely retried."""
        with self._connect(True) as connection:
            now = time.time()
            connection.execute("UPDATE profile_confirmations SET status='expired',mutations='[]' "
                               "WHERE status='pending' AND expires_at<=?", (now,))
            exhausted = connection.execute("SELECT run_id,dispatch_seq FROM memory_jobs WHERE attempts>=3 "
                "AND (status='pending' OR (status='processing' AND lease_until<?))", (now,)).fetchall()
            for item in exhausted:
                connection.execute("UPDATE memory_jobs SET status='failed',token=NULL,error='记忆提取重试次数已用尽',question='',output='{}' "
                                   'WHERE run_id=?', (item['run_id'],))
                enqueue_task(connection, 'memory.extract', item['run_id'], item['dispatch_seq'], dead=True)
            row = connection.execute(
                "SELECT j.* FROM memory_jobs j JOIN memory_settings s ON j.user_id=s.user_id "
                "WHERE s.enabled=1 AND s.revision=j.revision AND attempts<3 AND "
                "((status='pending' AND available<=?) OR (status='processing' AND lease_until<?)) "
                + ("AND j.run_id=? AND j.dispatch_seq=? " if run_id is not None else '') +
                "ORDER BY source_order,created_at LIMIT 1", (now, now, run_id, dispatch_seq) if run_id is not None else (now, now)).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            connection.execute("UPDATE memory_jobs SET status='processing',attempts=attempts+1,lease_until=?,token=? WHERE run_id=?",
                               (now + 60, token, row["run_id"]))
            return {**dict(row), "token": token, "attempts": row["attempts"] + 1}

    def defer(self, run_id: str, dispatch_seq: int) -> None:
        """Durably retain an unexecuted delivery while the prior bounded call drains."""
        with self._connect(True) as connection:
            available = time.time() + 5
            changed = connection.execute("UPDATE memory_jobs SET dispatch_seq=dispatch_seq+1,available=? "
                "WHERE run_id=? AND dispatch_seq=? AND status='pending' AND EXISTS "
                '(SELECT 1 FROM memory_settings s WHERE s.user_id=memory_jobs.user_id AND s.enabled=1 '
                'AND s.revision=memory_jobs.revision)', (available, run_id, dispatch_seq)).rowcount
            if changed:
                enqueue_task(connection, 'memory.extract', run_id, dispatch_seq+1, available)

    @staticmethod
    def _active(connection: sqlite3.Connection, job: dict) -> bool:
        row = connection.execute(
            "SELECT j.token,j.status,j.revision,s.revision current_revision,s.enabled FROM memory_jobs j "
            "JOIN memory_settings s ON s.user_id=j.user_id WHERE run_id=?", (job["run_id"],)).fetchone()
        return bool(row and row["status"] == "processing" and row["token"] == job["token"] and
                    row["enabled"] and row["revision"] == row["current_revision"])

    def fail(self, job: dict, retryable: bool = True) -> None:
        with self._connect(True) as connection:
            if not self._active(connection, job):
                return
            retry = retryable and job["attempts"] < 3
            delay = 1 if job["attempts"] == 1 else 5
            message = "记忆保存失败，可检查模型配置或存储；问答已独立保存"
            if not retry:
                try:
                    self._discard_pending(connection, job["run_id"])
                except (OSError, ApiError):
                    # Retain the pending index so explicit clear can retry actual
                    # file deletion, never lose track of a body we failed to erase.
                    message = "记忆保存失败且临时文件未能清理，请检查存储后清空记忆；问答已独立保存"
            available = time.time() + delay
            connection.execute("UPDATE memory_jobs SET status=?,available=?,token=NULL,error=?,dispatch_seq=dispatch_seq+?,"
                               "question=CASE WHEN ? THEN question ELSE '' END,"
                               "output=CASE WHEN ? THEN output ELSE '{}' END WHERE run_id=?",
                               ("pending" if retry else "failed", available,
                                message, int(retry), retry, retry, job["run_id"]))
            enqueue_task(connection, 'memory.extract', job['run_id'], job['dispatch_seq'] + int(retry),
                         available_at=available if retry else time.time(), dead=not retry)

    def _discard_pending(self, connection: sqlite3.Connection, run_id: str) -> None:
        pending = connection.execute("SELECT path FROM memory_episodes WHERE source_run_id=? AND status='pending'", (run_id,)).fetchone()
        if pending:
            self._remove_files(pending["path"])
            connection.execute("DELETE FROM memory_episodes WHERE source_run_id=? AND status='pending'", (run_id,))

    def episode_candidate(self, job: dict, raw: object, redacted: bool = False) -> dict | None:
        """Validate an episode, permanently ignoring legacy profile suggestions."""
        version = job.get("contract_version", 1)
        allowed = ({"episode"}, {"profiles", "episode"}) if version == 1 else ({"episode"},)
        if version not in (1, 2) or not isinstance(raw, dict) or set(raw) not in allowed:
            raise ValueError("Invalid extraction schema")
        episode = raw["episode"]
        if episode is not None:
            keys = {"title", "summary", "events", "decisions", "unresolved", "followups", "tags", "sensitive"}
            if not isinstance(episode, dict) or set(episode) != keys or not isinstance(episode["sensitive"], bool):
                raise ValueError("Invalid episode schema")
            _text(episode["title"], 128)
            _text(episode["summary"], 1000)
            for key in ("events", "decisions", "unresolved", "followups", "tags"):
                if not isinstance(episode[key], list) or len(episode[key]) > 8:
                    raise ValueError("Invalid episode list")
                for value in episode[key]:
                    _text(value, 128 if key == "tags" else 500)
            output = json.loads(job["output"])
            if output.get("abstained"):
                episode = None
            elif not job["consent"]:
                if episode["sensitive"]:
                    episode = None
                elif redacted:
                    # A redaction-pass summary is stored only after the server-side
                    # text gate passes; a model label alone never suffices.
                    episode = None if episode_text_sensitive(episode) else episode
                else:
                    episode = self._public_episode(job["question"])
            else:
                # Consent authorizes extraction, not a model's declassification.
                episode = {**episode, "sensitive": True}
        return episode

    @staticmethod
    def _safe_preference_supports(question: str) -> list[str]:
        """Derive minimal harmless phrases from input, never model profiles."""
        if re.search(r"假设|如果|家人|父亲|母亲|爸爸|妈妈|孩子|妻子|丈夫|他说|资料|病例|患者|例如|[\"“”]|"
                     r"\b(?:if|suppose|hypothetical|mother|father|parent|friend|patient|someone|example)\b",
                     question, re.IGNORECASE):
            return []
        values = sorted(set().union(*SAFE_PREFERENCES.values()), key=lambda value: (-len(value), value))
        pattern = r"(?:我(?:喜欢|偏好|希望)|请用|回答请用)(?:用)?(?:" + "|".join(map(re.escape, values)) + r")(?:回答)?"
        return list(dict.fromkeys(match.group(0) for match in re.finditer(pattern, question)))[:12]

    @staticmethod
    def _public_episode(question: str) -> dict | None:
        """Build unconsented episodes without retaining any free-form model prose."""
        # Both grammar AND a closed public-topic vocabulary are required. An
        # arbitrary topic can contain a person's name or private condition.
        generic = re.fullmatch(
            r"(?:请)?(?:介绍|解释|科普)(?P<topic>[\u4e00-\u9fff]{1,20})(?:的一般知识|的通用知识)[？?。！!]*", question)
        if generic and generic["topic"] in PUBLIC_TOPICS:
            topic = generic["topic"] + "：一般知识讨论"
            summary = "讨论公开资料的一般知识主题：" + generic["topic"] + "。非个人病史陈述，也不是医学证据。"
        elif supports := MemoryStore._safe_preference_supports(question):
            topic = "回答偏好讨论"
            summary = "用户明确提供的回答偏好：" + "；".join(supports)
        else:
            return None
        return {"title": topic[:128], "summary": summary[:1000], "events": [], "decisions": [],
                "unresolved": [], "followups": [], "tags": [], "sensitive": False}

    def complete(self, job: dict, candidates: object) -> None:
        """Compatibility entry point; background completion only writes episodes."""
        self.complete_episode(job, candidates)

    def complete_episode(self, job: dict, raw: object, redacted: bool = False) -> None:
        """Validate first, stage the file index, then commit under a revision lock."""
        episode = self.episode_candidate(job, raw, redacted)
        if episode is not None and not job["consent"] and episode_text_sensitive(episode):
            # Database write gate: unconsented episodes never persist sensitive text,
            # whatever upstream path produced or labeled the candidate.
            episode = None
        episode_id = _hash(job["run_id"])
        relative = f"{_hash(USER)[:16]}/{_hash(job['conversation_id'])[:24]}/{episode_id}.md"
        if episode:
            # Persist the pending path before touching the file: clear can remove it
            # even if a process dies after atomic replacement but before final commit.
            with self._connect(True) as connection:
                if not self._active(connection, job):
                    return
                connection.execute("INSERT OR IGNORE INTO memory_episodes VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (episode_id, USER, relative, episode["title"], json.dumps(episode["tags"], ensure_ascii=False),
                     job["created_at"], job["conversation_id"], job["run_id"], episode["sensitive"], "pending"))
        with self._connect(True) as connection:
            if not self._active(connection, job):
                return
            writes = 0
            if episode:
                content = self._markdown(job, episode_id, episode)
                self._write(relative, content)
                connection.execute("UPDATE memory_episodes SET status='ready' WHERE id=?", (episode_id,))
                writes += 1
            if episode is None:
                self._discard_pending(connection, job["run_id"])
            connection.execute("UPDATE memory_jobs SET status=?,token=NULL,error=NULL,question='',output='{}' WHERE run_id=?",
                               ("saved" if writes else "skipped", job["run_id"]))

    @staticmethod
    def _markdown(job: dict, episode_id: str, episode: dict) -> str:
        metadata = {"version": 1, "id": episode_id, "user_id": USER, "created_at": job["created_at"],
                    "source_conversation_id": job["conversation_id"], "source_run_id": job["run_id"],
                    "sensitive": episode["sensitive"], "provenance": "历史问答摘要，非医学证据；用户健康自述未经核实"}
        text = "<!-- memory-v1\n" + json.dumps(metadata, ensure_ascii=False) + "\n-->\n"
        text += f"# {episode['title']}\n\n{episode['summary']}\n"
        for key, label in (("events", "事件"), ("decisions", "决定"), ("unresolved", "未解决问题"), ("followups", "后续事项")):
            text += f"\n## {label}\n" + "\n".join(f"- {item}" for item in episode[key]) + "\n"
        return text

    def _write(self, relative: str, content: str) -> None:
        if len(content.encode("utf-8")) > 32768:
            raise ValueError("Episode too large")
        path = self._file(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after mkdir, including user-created directory symlinks/junctions.
        path = self._file(relative)
        # A deterministic, exclusively created temp path can be recovered by
        # clear/delete after a crash; the database lock serializes every writer.
        temporary = path.with_name(f".memory-{path.stem}.tmp")
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_files(self, relative: str) -> None:
        path = self._file(relative)
        # Only these two exact, root-checked files belong to this index entry.
        path.unlink(missing_ok=True)
        path.with_name(f".memory-{path.stem}.tmp").unlink(missing_ok=True)
