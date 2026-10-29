"""Atomic, versioned profile transactions and single-use confirmations."""

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import asdict

from .memory import PROFILE_KEYS, USER, MemoryStore, _profile
from .profile_models import (
    Mutation,
    ProfileAction,
    ProfileOutcome,
    ProfilePlan,
    ProfileSnapshot,
)
from .types import ApiError

LABELS = {"display_name": "称呼", "occupation": "职业", "language": "语言偏好",
          "response_style": "回答风格", "interests": "兴趣", "goals": "目标", "health": "健康自述"}


def fingerprint(value: object) -> str:
    """Bind a request to canonical JSON without copying its raw private body."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class ProfileStore:
    """Use the authoritative memory connection; never hold it during model calls."""

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def snapshot(self) -> ProfileSnapshot:
        with self.memory._connect() as connection:
            connection.execute("BEGIN")
            settings = self.memory._settings(connection)
            version = connection.execute("SELECT revision FROM profile_versions WHERE user_id=?", (USER,)).fetchone()
            rows = connection.execute("SELECT * FROM memory_profiles WHERE user_id=?", (USER,))
            return ProfileSnapshot(bool(settings["enabled"]), version[0], settings["revision"],
                                   {row["key"]: self.memory._public_profile(row) for row in rows}
                                   if settings["enabled"] else {})

    @staticmethod
    def _replay(connection: sqlite3.Connection, request_id: str, digest: str) -> ProfileOutcome | None:
        row = connection.execute("SELECT * FROM profile_requests WHERE request_id=? AND user_id=?",
                                 (request_id, USER)).fetchone()
        if row and row["fingerprint"] != digest:
            raise ApiError("REQUEST_ID_CONFLICT", "请求标识已用于不同内容", 409)
        return ProfileOutcome.model_validate_json(row["outcome"]) if row else None

    @staticmethod
    def _remember(connection: sqlite3.Connection, request_id: str, digest: str,
                  outcome: ProfileOutcome) -> ProfileOutcome:
        connection.execute("INSERT INTO profile_requests VALUES (?,?,?,?,?)",
                           (request_id, USER, digest, outcome.model_dump_json(), time.time()))
        return outcome

    def request_outcome(self, request_id: str, question: str, conversation_id: str,
                        consent: bool) -> ProfileOutcome | None:
        """Recover a committed write without repeating extraction after a crash."""
        with self.memory._connect() as connection:
            return self._replay(connection, request_id, fingerprint([question, conversation_id, consent]))

    @staticmethod
    def _owner(connection: sqlite3.Connection, request_id: str, owner_token: str | None) -> None:
        if owner_token is None:
            return
        row = connection.execute("SELECT owner_token,state FROM chat_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row or row["owner_token"] != owner_token or row["state"] != "processing":
            raise ApiError("REQUEST_LEASE_LOST", "请求已被接管，请重试读取结果", 409)

    @staticmethod
    def _subtraction(connection: sqlite3.Connection, item: Mutation) -> bool:
        """Keeping only exact existing facts is erasure, not new sensitive saving."""
        if item.operation != "set" or item.source != "explicit" or item.key not in {"health", "interests", "goals"}:
            return False
        row = connection.execute("SELECT value FROM memory_profiles WHERE key=? AND user_id=?", (item.key, USER)).fetchone()
        if row is None:
            return False
        before = json.loads(row[0]).split("；")
        after = item.value.split("；")
        return bool(item.value and len(after) < len(before) and
                    len(set(before)) == len(before) and len(set(after)) == len(after) and
                    "；".join(fact for fact in before if fact in after) == item.value)

    def _commit(self, connection: sqlite3.Connection, mutations: tuple[Mutation, ...],
                conversation: str, run_id: str, order: int, consent: bool) -> list[ProfileAction]:
        # Validate the whole batch before touching any profile. Plans are local,
        # but checks here prevent a future caller bypassing the authorization gate.
        if len(mutations) > 12 or len({m.key for m in mutations}) != len(mutations):
            raise ApiError("INVALID_MEMORY", "画像操作重复或数量超过限制", 422)
        for item in mutations:
            if item.key not in PROFILE_KEYS or item.operation not in {"set", "delete"}:
                raise ApiError("INVALID_MEMORY", "画像字段或操作不符合要求", 422)
            if item.operation == "set":
                try:
                    _profile(item.key, item.value, PROFILE_KEYS[item.key], item.sensitive)
                except (TypeError, ValueError) as error:
                    raise ApiError("INVALID_MEMORY", "画像字段、长度或敏感授权不符合要求", 422) from error
                if item.sensitive and not consent and not self._subtraction(connection, item):
                    raise ApiError("MEMORY_CONSENT_REQUIRED", "敏感画像保存需要本次授权", 422)
        changed = deleted = False
        actions = []
        for item in mutations:
            row = connection.execute("SELECT * FROM memory_profiles WHERE key=? AND user_id=?",
                                     (item.key, USER)).fetchone()
            label = LABELS[item.key]
            if item.operation == "delete":
                status = "deleted" if row else "unchanged"
                if row:
                    connection.execute("DELETE FROM memory_profiles WHERE key=? AND user_id=?", (item.key, USER))
                    changed = deleted = True
                message = f"{label}画像已删除；不删除聊天历史或情景记忆" if row else f"没有记录{label}画像"
            elif row and json.loads(row["value"]) == item.value and bool(row["sensitive"]) == item.sensitive:
                status, message = "unchanged", f"已记录相同的{label}，无需更新"
            else:
                deleted = deleted or self._subtraction(connection, item)
                status = "updated" if row else "added"
                self.memory._upsert_profile(connection, item.key, item.value, PROFILE_KEYS[item.key],
                                            item.sensitive, item.source, item.support, conversation, run_id, order)
                changed = True
                message = f"{label}画像已更新" if row else f"{label}画像已新增"
            actions.append(ProfileAction(key=item.key, status=status, message=message))
        if changed:
            self.memory._profile_changed(connection, order)
        if deleted:
            self.memory._bump(connection)
        return actions

    def apply(self, request_id: str, question: str, conversation_id: str, run_id: str,
              source_order: int, plan: ProfilePlan, consent: bool,
              owner_token: str | None = None) -> ProfileOutcome:
        digest = fingerprint([question, conversation_id, consent])
        with self.memory._connect(True) as connection:
            self._owner(connection, request_id, owner_token)
            previous = self._replay(connection, request_id, digest)
            if previous is not None:
                return previous
            settings = self.memory._settings(connection)
            version = connection.execute("SELECT * FROM profile_versions WHERE user_id=?", (USER,)).fetchone()
            if not settings["enabled"]:
                result = ProfileOutcome(status="disabled")
            elif (version["revision"] != plan.profile_revision or settings["revision"] != plan.privacy_revision
                  or source_order < version["last_source_order"]):
                result = ProfileOutcome(status="conflict", actions=[ProfileAction(
                    key="*", status="conflict", message="画像已变更，本次未写入，请重新发送")])
            else:
                committed_keys = {m.key for m in plan.mutations}
                actions = [a for a in plan.actions if not (a.key in committed_keys and
                           a.status in {"added", "updated", "deleted", "unchanged"})]
                actions.extend(self._commit(connection, plan.mutations, conversation_id, run_id, source_order, consent))
                result = ProfileOutcome(status="processed" if actions or plan.confirmations else "skipped", actions=actions)
                if plan.confirmations:
                    required = any(m.operation == "set" and m.sensitive and not self._subtraction(connection, m)
                                   for m in plan.confirmations)
                    if required and not consent:
                        raise ApiError("MEMORY_CONSENT_REQUIRED", "敏感画像保存需要本次授权", 422)
                    token = secrets.token_urlsafe(32)
                    expires = time.time() + 300
                    version = connection.execute("SELECT revision FROM profile_versions WHERE user_id=?", (USER,)).fetchone()[0]
                    privacy = self.memory._settings(connection)["revision"]
                    body = {"mutations": [asdict(m) for m in plan.confirmations],
                            "source_order": source_order, "run_id": run_id}
                    encoded = json.dumps(body, ensure_ascii=False)
                    if len(encoded) > 32768:
                        raise ApiError("INVALID_MEMORY", "画像确认内容超过限制", 422)
                    connection.execute("INSERT INTO profile_confirmations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (fingerprint(token), USER, request_id, conversation_id, version, privacy, encoded,
                         required, expires, "pending", None))
                    for item in plan.confirmations:
                        if not any(a.key == item.key and a.status == "needs_confirmation" for a in result.actions):
                            result.actions.append(ProfileAction(key=item.key, status="needs_confirmation",
                                                                message=f"请确认{LABELS.get(item.key, '全部')}画像变更"))
                    result.confirmation_token = token
                    result.confirmation_expires_at = expires
                    result.confirmation_requires_consent = required
            return self._remember(connection, request_id, digest, result)

    def confirm(self, token: str, request_id: str, conversation_id: str,
                accept: bool, consent: bool, owner_token: str | None = None) -> ProfileOutcome:
        digest = fingerprint([token, conversation_id, accept, consent])
        with self.memory._connect(True) as connection:
            self._owner(connection, request_id, owner_token)
            previous = self._replay(connection, request_id, digest)
            if previous is not None:
                return previous
            row = connection.execute("SELECT * FROM profile_confirmations WHERE token_hash=? AND user_id=?",
                                     (fingerprint(token), USER)).fetchone()
            if row is None or row["conversation_id"] != conversation_id:
                raise ApiError("MEMORY_CONFIRMATION_INVALID", "画像确认不存在或不属于本会话", 409)
            if row["status"] != "pending" or row["expires_at"] <= time.time():
                raise ApiError("MEMORY_CONFIRMATION_EXPIRED", "画像确认已过期、取消或处理，请重新发起", 409)
            settings = self.memory._settings(connection)
            version = connection.execute("SELECT revision FROM profile_versions WHERE user_id=?", (USER,)).fetchone()[0]
            if version != row["profile_revision"] or settings["revision"] != row["privacy_revision"] or not settings["enabled"]:
                raise ApiError("MEMORY_CONFIRMATION_CONFLICT", "画像已变更，请重新发起确认", 409)
            if accept and row["requires_consent"] and not consent:
                raise ApiError("MEMORY_CONSENT_REQUIRED", "敏感画像保存需要本次确认授权", 422)
            if accept:
                body = json.loads(row["mutations"])
                mutations = tuple(Mutation(**item) for item in body["mutations"])
                actions = self._commit(connection, mutations, conversation_id, body["run_id"], body["source_order"], consent)
            else:
                actions = [ProfileAction(key="*", status="unchanged", message="已取消画像变更")]
            result = ProfileOutcome(status="processed", actions=actions)
            connection.execute("UPDATE profile_confirmations SET status=?,mutations='[]',outcome=? WHERE token_hash=?",
                               ("confirmed" if accept else "cancelled", result.model_dump_json(), fingerprint(token)))
            return self._remember(connection, request_id, digest, result)
