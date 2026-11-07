"""Application-level composable profile maintenance and evidence-based answers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol

from fastapi.encoders import jsonable_encoder

from .chat_history import ChatHistoryStore
from .dialogue_models import DialogueRun, DialogueSnapshot
from .memory import MemoryStore
from .profile_models import (
    IntentDecision,
    ProfileAction,
    ProfileOutcome,
    ProfileSnapshot,
)
from .profile_policy import build_profile_plan
from .profile_store import LABELS, ProfileStore, fingerprint
from .public_errors import public_error
from .short_term import ShortTerm, ShortTermMemory
from .types import ApiError
from .workflow.contracts import StreamEmitter

if TYPE_CHECKING:
    from .chat_api import ChatRunRequest, ConfirmationRequest
    from .service import RagService

logger = logging.getLogger(__name__)
LEASE_RENEW_SECONDS = 10


class IntentAnalyzer(Protocol):
    """Injectable bounded classifier contract, shared by production and tests."""

    def analyze(self, question: str, snapshot: ProfileSnapshot) -> IntentDecision: ...


def _fallback_needs_answer(question: str) -> bool:
    """Only recognize finite explicit profile-only forms when analysis is disabled."""
    fields = r"职业|姓名|名字|称呼|语言偏好|回答风格|兴趣|爱好|目标|健康画像|画像|长期记忆"
    if re.fullmatch(r"(?:你(?:还)?记得)?我的(?:" + fields + r")(?:是什么|有哪些|是什么来着|吗|么|呢)?[？?。！!]*", question):
        return False
    if re.search(r"[？?]|吗|么|呢|如何|怎么|为什么|能否|是否|可否|建议|请(?:问|解释|介绍|科普|分析|回答|帮)|告诉我", question):
        return True
    # A leading self-report cannot swallow a later independent question or task.
    clauses = [part.strip() for part in re.split(r"[，,；;。.!\n]+", question) if part.strip()]
    pure = (
        r"(?:(?:请)?(?:记住|记下|记得|忘记|忘掉|删除|清空|清除|撤回|更正|修改|更新)"
        r"[^，,；;。.!\n]{0,60}(?:我|画像|记忆|职业|名字|偏好|兴趣|目标)[^，,；;。.!\n]{0,500}|"
        r"(?:我叫|我的名字是|我是(?:一名|一个)?|我的职业是|我的目标是|我(?:喜欢|偏好|关注|患有|有|确诊为)|请叫我|请用)"
        r"[^，,；;。.!\n]{1,500})"
    )
    return not bool(clauses and all(re.fullmatch(pure, part) for part in clauses))


class ChatRuntime:
    """Persist routing, profile transactions, and final results with stable IDs."""

    def __init__(self, history: ChatHistoryStore, memory: MemoryStore,
                 analyzer: IntentAnalyzer, service_factory: Callable[[], RagService],
                 short_term: object | None = None,
                 summarizer: object | None = None,
                 indexer: object | None = None,
                 *, dialogue_state_enabled: bool = False) -> None:
        self.history, self.memory = history, memory
        self.profiles = ProfileStore(memory)
        self.analyzer, self.service_factory = analyzer, service_factory
        self.short_term = short_term or ShortTermMemory(history)
        self.summarizer = summarizer
        self.indexer = indexer
        # Trusted process configuration only: a browser-supplied input must never be
        # able to turn the structured-state path on for one request.
        self.dialogue_state_enabled = dialogue_state_enabled

    @contextmanager
    def _lease(self, request_id: str, owner: str) -> Iterator[None]:
        stop = threading.Event()

        def renew() -> None:
            while not stop.wait(LEASE_RENEW_SECONDS):
                try:
                    if not self.history.renew_request(request_id, owner):
                        return
                except Exception:  # noqa: BLE001 - final transaction still checks ownership
                    return

        thread = threading.Thread(target=renew, name="rag-chat-lease", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1)

    @staticmethod
    def _progress(emit: StreamEmitter | None, stage: str) -> None:
        if emit:
            emit({"event": "chat.progress", "data": {"stage": stage}})

    @staticmethod
    def _record(run_id: str, workflow_id: str | None, profile: ProfileOutcome) -> dict:
        return {"run_id": run_id, "workflow_id": workflow_id or "", "status": "success",
                "trace": [], "output": {"answer": "", "citations": [], "abstained": False},
                "kind": "memory", "profile": profile.model_dump(),
                "episode": {"status": "skipped"}, "memory": {"status": "skipped"}}

    def _finish(self, payload: ChatRunRequest, request: dict, record: dict, revision: int | None,
                answer_ran: bool, dialogue: DialogueRun | None = None) -> dict:
        clarification = record["output"].get("answer_type") == "clarification"
        callback = None
        if answer_ran and record["status"] == "success" and not clarification and (
                record["output"].get("answer") or record["output"].get("abstained")):
            record["episode"] = record["memory"] = {"status": "failed" if revision is None else "pending"}
            if revision is not None:
                def enqueue(connection: sqlite3.Connection) -> None:
                    self.memory.enqueue(connection, request["run_id"], payload.conversation_id,
                                        request["turn_id"], payload.inputs["question"], record["output"],
                                        revision, payload.memory_consent)
                    row = connection.execute("SELECT status FROM memory_jobs WHERE run_id=?",
                                             (request["run_id"],)).fetchone()
                    record["episode"] = record["memory"] = {"status": row[0] if row else "skipped"}

                callback = enqueue
        self.history.finish_turn(payload.conversation_id, request["turn_id"], record,
                                 on_saved=callback, request_id=payload.request_id,
                                 owner_token=request["owner_token"], dialogue=dialogue,
                                 invalidate_state=answer_ran and not self.dialogue_state_enabled)
        if self.indexer is not None and record.get("kind") != "memory" and not clarification:
            try:
                self.indexer.maybe_schedule(payload.conversation_id, request["turn_id"],
                                            payload.inputs["question"],
                                            record["output"].get("answer"))
            except Exception:  # noqa: BLE001 - the turn index is an optional read model
                logger.warning("Conversation index unavailable")
        if self.summarizer is not None:
            try:
                self.summarizer.maybe_schedule(payload.conversation_id)
            except Exception:  # noqa: BLE001 - digest maintenance never fails a turn
                logger.warning("Conversation digest unavailable")
        return record

    def delete_conversation(self, conversation_id: str) -> dict:
        """Delete one conversation and every derived copy of it.

        Order matters: history rows are the source of truth and are deleted first,
        so a failure afterwards can only leave an orphaned read model, never a turn
        whose vectors or episodes claim content the user already removed.
        """
        run_ids = self.history.delete_conversation(conversation_id)
        if run_ids is None:
            raise ApiError("CONVERSATION_NOT_FOUND", "对话记录不存在", 404)
        memory = {"jobs": 0, "episodes": 0, "confirmations": 0}
        try:
            memory = self.memory.delete_conversation(conversation_id, run_ids)
        except Exception:  # noqa: BLE001 - optional store, conversation is already gone
            logger.warning("Conversation memory cleanup unavailable")
        try:
            self.service_factory().forget_runs(run_ids)
        except Exception:  # noqa: BLE001 - the in-memory cache is not authoritative
            logger.warning("Conversation run cache cleanup unavailable")
        vectors = "disabled"
        if self.indexer is not None:
            vectors = "deleted" if self.indexer.delete(conversation_id) else "unavailable"
        return {"conversation_id": conversation_id, "deleted": True,
                "memory": memory, "vectors": vectors}

    def run(self, payload: ChatRunRequest, emit: StreamEmitter | None = None) -> dict:
        """Apply grounded profile changes before the unchanged evidence question."""
        question = payload.inputs["question"]
        initial = None
        try:
            initial = self.profiles.snapshot()
        except Exception:  # noqa: BLE001 - optional memory may be unavailable before claiming a chat
            logger.warning("Profile snapshot unavailable")
        boundary = {"profile_revision": initial.profile_revision if initial else None,
                    "privacy_revision": initial.privacy_revision if initial else None}
        request = self.history.begin_request(payload.conversation_id, payload.request_id, question,
                                             fingerprint(["chat", payload.model_dump()]), boundary)
        if request["state"] == "completed":
            return request["result"]
        with self._lease(payload.request_id, request["owner_token"]):
            self._progress(emit, "intent")
            decision = IntentDecision(needs_answer=True)
            profile = ProfileOutcome(status="failed", actions=[ProfileAction(
                key="*", status="failed", message="画像识别或存储暂不可用，本次未新增画像")])
            try:
                saved = self.profiles.request_outcome(payload.request_id, question, payload.conversation_id,
                                                      payload.memory_consent)
                if saved is not None and request.get("preflight"):
                    routing = json.loads(request["preflight"])
                    decision = IntentDecision.model_validate(routing["routing"])
                    profile = saved
                else:
                    if initial is None:
                        raise ValueError("Profile snapshot unavailable")
                    snapshot = initial
                    original = json.loads(request["preflight"] or "{}")
                    if (original.get("privacy_revision") != snapshot.privacy_revision or
                            original.get("profile_revision") != snapshot.profile_revision):
                        decision = IntentDecision.model_validate(original["routing"]) if original.get("routing") else IntentDecision(
                            needs_answer=_fallback_needs_answer(question))
                        profile = ProfileOutcome(status="conflict", actions=[ProfileAction(
                            key="*", status="conflict", message="请求中断期间画像或隐私设置已变更，旧消息未重写画像，请重新发送")])
                    elif not snapshot.enabled:
                        decision = IntentDecision(needs_answer=_fallback_needs_answer(question))
                        profile = ProfileOutcome(status="disabled")
                    else:
                        decision = self.analyzer.analyze(question, snapshot)
                        if not isinstance(decision, IntentDecision):
                            raise ValueError("Invalid intent contract")
                        # Routing only: no assertions or sensitive fact copies in recovery metadata.
                        original["routing"] = decision.model_dump(exclude={"candidates"})
                        self.history.save_preflight(payload.request_id, request["owner_token"], original)
                        self._progress(emit, "profile")
                        profile = self.profiles.apply(payload.request_id, question, payload.conversation_id,
                            request["run_id"], request["turn_id"], build_profile_plan(
                                question, decision, snapshot, payload.memory_consent), payload.memory_consent,
                            owner_token=request["owner_token"])
            except ApiError as error:
                if public_error(error.code).status == 409:
                    raise
            except Exception:  # noqa: BLE001 - optional extraction failures must not block retrieval
                logger.warning("Profile analysis unavailable")
            record = self._record(request["run_id"], payload.workflow_id, profile)
            memory_intent = bool(decision.candidates or decision.lookup_keys or decision.clarification or
                                 profile.actions or not decision.needs_answer)
            record["kind"] = ("mixed" if memory_intent else "answer") if decision.needs_answer else "memory"
            revision, context = None, ""
            try:
                revision, context = self.memory.snapshot(question)
            except Exception:  # noqa: BLE001 - evidence QA remains independent of optional memory
                logger.warning("Memory recall unavailable")
            # One consistent read serves both the legacy generation context and the
            # request-scoped resolution slot; two separate reads could disagree.
            dialogue, short_term = self._dialogue_snapshot(payload.conversation_id)
            if decision.needs_answer:
                self._progress(emit, "answer")
                try:
                    if not payload.workflow_id:
                        raise ApiError("WORKFLOW_REQUIRED", "请先选择已发布的问答工作流；画像处理结果已保留", 422)
                    result = asyncio.run(self.service_factory().run_workflow(
                        payload.workflow_id, payload.inputs, emit=emit, memory_context=context,
                        run_id=request["run_id"],
                        short_term=short_term,
                        conversation_id=payload.conversation_id,
                        dialogue=dialogue))
                    answer = jsonable_encoder(result)
                    record.update(answer)
                    # Workflow outputs cannot override the application-level result envelope.
                    record["run_id"] = request["run_id"]
                except Exception as error:  # noqa: BLE001 - preserve an already committed profile outcome
                    record["status"] = "failed"
                    record["error"] = public_error(error.code if isinstance(error, ApiError) else None).data()
            else:
                lines = [a.message for a in profile.actions]
                if decision.lookup_keys:
                    try:
                        current = self.profiles.snapshot()
                        lines.extend(f"{LABELS[key]}：" + (str(current.profiles[key]["value"]) if
                                     current.enabled and key in current.profiles else "未记录或记忆已关闭")
                                     for key in decision.lookup_keys)
                    except Exception:  # noqa: BLE001 - never fall back to episodes for profile lookup
                        lines.append("画像查询暂不可用")
                if decision.clarification:
                    lines.append(decision.clarification)
                if profile.status == "disabled":
                    lines.append("长期记忆已关闭，本次未写入")
                record["output"]["answer"] = "\n".join(lines) or "未发现可维护的画像信息，请补充说明"
            return self._finish(payload, request, record, revision, decision.needs_answer,
                                dialogue if decision.needs_answer else None)

    def _dialogue_snapshot(self, conversation_id: str) -> tuple[DialogueRun | None, ShortTerm]:
        """Load the one snapshot this request may use, degrading to empty context.

        With the feature enabled the structured snapshot is authoritative and the
        legacy window is built from it, so the generator and the reducer cannot see
        two different histories. With it disabled the old reader stays untouched.
        """
        if not self.dialogue_state_enabled:
            return None, self.short_term.load(conversation_id)
        try:
            snapshot = self.history.dialogue_snapshot(conversation_id)
        except Exception:  # noqa: BLE001 - optional context never blocks an answer
            logger.warning("Dialogue snapshot unavailable")
            return DialogueRun(DialogueSnapshot()), ShortTerm()
        short_term = ShortTerm(
            recent=tuple({"turn": turn.turn_id, "user": turn.user, "assistant": turn.assistant}
                         for turn in snapshot.recent),
            summary=snapshot.summary)
        return DialogueRun(snapshot), short_term

    def confirm(self, payload: ConfirmationRequest) -> dict:
        """Consume a server-owned plan without reanalysis or repeating the question."""
        question = "确认画像变更" if payload.accept else "取消画像变更"
        request = self.history.begin_request(payload.conversation_id, payload.request_id, question,
                                             fingerprint(["confirm", payload.model_dump()]))
        if request["state"] == "completed":
            return request["result"]
        with self._lease(payload.request_id, request["owner_token"]):
            try:
                profile = self.profiles.confirm(payload.token, payload.request_id, payload.conversation_id,
                                                payload.accept, payload.memory_consent,
                                                owner_token=request["owner_token"])
            except ApiError as error:
                self.history.release_request(payload.request_id, request["owner_token"])
                safe_error = public_error(error.code)
                if safe_error.code in {"MEMORY_CONFIRMATION_EXPIRED", "MEMORY_CONFIRMATION_CONFLICT"}:
                    self.history.resolve_confirmation(payload.token, ProfileOutcome(status="conflict", actions=[
                        ProfileAction(key="*", status="conflict", message=safe_error.message)]).model_dump())
                raise
            self.history.resolve_confirmation(payload.token, profile.model_dump())
            record = self._record(request["run_id"], None, profile)
            record["output"]["answer"] = "\n".join(a.message for a in profile.actions)
            self.history.finish_turn(payload.conversation_id, request["turn_id"], record,
                                     request_id=payload.request_id, owner_token=request["owner_token"])
            return record
