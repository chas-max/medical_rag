"""Local chat integration: snapshot in, verified resolution, atomic commit."""

import asyncio
import json

import pytest

from medical_rag.chat_api import ChatRunRequest, stream_response
from medical_rag.chat_history import ChatHistoryStore
from medical_rag.chat_runtime import ChatRuntime
from medical_rag.dialogue_models import DialogueRun
from medical_rag.memory import MemoryStore
from medical_rag.profile_models import IntentDecision
from medical_rag.question_resolver import QuestionResolver, clarification_answer
from medical_rag.types import ApiError


class Analyzer:
    def analyze(self, question, snapshot):
        return IntentDecision(needs_answer=True)


class DialogueTransport:
    """Ground-truth-free fake that resolves from the snapshot it is handed."""

    def __init__(self):
        self.calls = []

    def complete_with_timeout(self, provider, system, user, timeout):
        payload = json.loads(user)
        self.calls.append(payload)
        question, context = payload["question"], payload["context"]
        previous = (context.get("state") or {}).get("active_topic") or {}
        name = previous.get("name")
        if "糖尿病" in question:
            name = "糖尿病"
        elif "高血压" in question:
            name = "高血压"
        source = "current" if name and name in question else previous.get("source_turn_id")
        cause = "引起" in question
        resolution = {
            "status": "resolved",
            "relation": "standalone" if not context.get("state") else
                        ("topic_switch" if source == "current" else "follow_up"),
            "resolved_question": (f"{name}是由什么引起的？" if cause
                                  else f"{name}在日常生活中需要注意什么？"),
            "topic": ({"name": name, "type": "disease", "source_turn_id": source,
                       "source_role": "user"} if name else None),
            "intent": "etiology" if cause else "daily_precautions",
            "intent_text": "由什么引起" if cause else "日常注意事项",
            "references": [], "clarification": None,
        }
        return json.dumps(resolution, ensure_ascii=False)


class AnswerService:
    """Same contract as ``RagService.run_workflow``, with a real resolver."""

    def __init__(self, transport, outcome="answer"):
        self.transport = transport
        self.outcome = outcome
        self.snapshots = []

    async def run_workflow(self, workflow_id, inputs, emit=None, memory_context="",
                           run_id=None, short_term=None, conversation_id="",
                           dialogue=None, retain_run=True):
        assert dialogue is None or isinstance(dialogue, DialogueRun)
        if dialogue is None:
            return {"run_id": run_id, "workflow_id": workflow_id, "status": "success",
                    "trace": [], "output": {"answer": "合成回答", "citations": [],
                                            "abstained": False}}
        self.snapshots.append(dialogue.snapshot)
        resolution = QuestionResolver(self.transport).resolve(inputs["question"],
                                                            dialogue.snapshot)
        dialogue.resolution = resolution
        if self.outcome == "failed":
            return {"run_id": run_id, "workflow_id": workflow_id, "status": "failed",
                    "trace": [], "output": {}}
        if resolution.status == "clarification":
            output = clarification_answer(resolution)
        elif self.outcome == "abstain":
            output = {"answer": "现有资料不足。", "citations": [], "abstained": True,
                      "abstain_reason": "ABSTAIN_NO_EVIDENCE"}
        elif self.outcome == "truncated":
            output = {"answer": "生成异常：模型输出被截断。", "citations": [], "abstained": True,
                      "abstain_reason": "ABSTAIN_TRUNCATED"}
        else:
            output = {"answer": f"合成回答：{resolution.resolved_question}",
                      "citations": [], "abstained": False}
        return {"run_id": run_id, "workflow_id": workflow_id, "status": "success",
                "trace": [], "output": output}


class Indexer:
    def __init__(self):
        self.scheduled = []

    def maybe_schedule(self, conversation_id, turn_id, question, answer):
        self.scheduled.append(question)


def make_runtime(tmp_path, service, *, enabled=True, path=None):
    history = ChatHistoryStore(path or tmp_path / "history.sqlite3")
    memory = MemoryStore(path or tmp_path / "history.sqlite3", tmp_path / "episodes")
    return ChatRuntime(history, memory, Analyzer(), lambda: service,
                       dialogue_state_enabled=enabled)


def ask(runtime, question, request_id, conversation="c1"):
    return runtime.run(ChatRunRequest(request_id=request_id, conversation_id=conversation,
                                      inputs={"question": question}))


def three_turns(runtime):
    ask(runtime, "高血压在日常生活中需要注意什么？", "r1")
    ask(runtime, "糖尿病呢？", "r2")
    return ask(runtime, "那它是由什么引起的？", "r3")


def test_three_turns_persist_and_a_new_runtime_can_continue(tmp_path):
    transport = DialogueTransport()
    runtime = make_runtime(tmp_path, AnswerService(transport))
    three_turns(runtime)

    snapshot = runtime.history.dialogue_snapshot("c1")
    assert snapshot.state.active_topic.name == "糖尿病"
    assert snapshot.state.active_intent == "etiology"
    assert snapshot.state.revision == 3

    # A fresh process reads the same subject from SQLite and resolves the next
    # anaphor without any caller-held state.
    again = make_runtime(tmp_path, AnswerService(transport))
    result = ask(again, "那它有什么症状？", "r4")

    assert result["status"] == "success"
    assert again.history.dialogue_snapshot("c1").state.active_topic.name == "糖尿病"


def test_the_current_question_is_never_in_its_own_recent_window(tmp_path):
    service = AnswerService(DialogueTransport())
    runtime = make_runtime(tmp_path, service)
    three_turns(runtime)
    ask(runtime, "那它有什么症状？", "r4")

    latest = service.snapshots[-1]
    assert [turn.user for turn in latest.recent] == [
        "高血压在日常生活中需要注意什么？", "糖尿病呢？", "那它是由什么引起的？"]
    assert all("症状" not in turn.user for turn in latest.recent)


def test_technical_failure_does_not_advance_the_topic(tmp_path):
    service = AnswerService(DialogueTransport())
    runtime = make_runtime(tmp_path, service)
    ask(runtime, "糖尿病在日常需要注意什么？", "r1")
    before = runtime.history.dialogue_snapshot("c1").state

    service.outcome = "failed"
    ask(runtime, "那它由什么引起？", "r2")

    after = runtime.history.dialogue_snapshot("c1").state
    assert after.active_topic.name == before.active_topic.name
    assert after.revision == before.revision


def test_meaningful_abstention_still_records_the_subject(tmp_path):
    service = AnswerService(DialogueTransport(), outcome="abstain")
    runtime = make_runtime(tmp_path, service)

    ask(runtime, "糖尿病在日常需要注意什么？", "r1")

    assert runtime.history.dialogue_snapshot("c1").state.active_topic.name == "糖尿病"


def test_truncated_generation_is_not_treated_as_success(tmp_path):
    service = AnswerService(DialogueTransport(), outcome="truncated")
    runtime = make_runtime(tmp_path, service)

    ask(runtime, "糖尿病在日常需要注意什么？", "r1")

    assert runtime.history.dialogue_snapshot("c1").state is None


def test_clarification_skips_memory_and_the_turn_index(tmp_path):
    service = AnswerService(DialogueTransport())
    runtime = make_runtime(tmp_path, service)
    runtime.indexer = Indexer()

    result = ask(runtime, "它是什么引起的？", "r1")

    assert result["output"]["answer_type"] == "clarification"
    assert runtime.indexer.scheduled == []
    assert result["memory"] == {"status": "skipped"}
    assert result["episode"] == {"status": "skipped"}
    stored = runtime.history.get_conversation("c1")["messages"][-1]
    assert stored["answerType"] == "clarification"


def test_disabled_switch_keeps_the_legacy_path(tmp_path):
    service = AnswerService(DialogueTransport())
    runtime = make_runtime(tmp_path, service, enabled=False)

    result = ask(runtime, "糖尿病在日常需要注意什么？", "r1")

    assert result["status"] == "success"
    assert runtime.history.dialogue_snapshot("c1").state is None
    assert runtime.history.dialogue_snapshot("c1").recent[0].user == "糖尿病在日常需要注意什么？"


def test_a_stale_local_snapshot_is_rejected_before_any_answer_is_kept(tmp_path):
    service = AnswerService(DialogueTransport())
    runtime = make_runtime(tmp_path, service)
    ask(runtime, "糖尿病在日常需要注意什么？", "r1")

    stale = runtime.history.dialogue_snapshot("c1")
    turn = runtime.history.begin_turn("c1", "并发写入的问题")
    run = DialogueRun(stale)
    run.resolution = QuestionResolver(DialogueTransport()).resolve(
        "糖尿病在日常需要注意什么？", stale)

    with pytest.raises(ApiError) as caught:
        runtime.history.finish_turn("c1", turn, {
            "run_id": "r-stale", "status": "success", "trace": [],
            "output": {"answer": "合成回答", "citations": [], "abstained": False}},
            dialogue=run)
    assert caught.value.code == "CONVERSATION_CONTEXT_CHANGED"


def test_stream_done_is_emitted_only_after_the_state_commit(tmp_path):
    service = AnswerService(DialogueTransport())
    runtime = make_runtime(tmp_path, service)
    payload = ChatRunRequest(request_id="r1", conversation_id="c1",
                             inputs={"question": "糖尿病在日常需要注意什么？"})

    async def collect():
        response = stream_response(lambda emit: runtime.run(payload, emit), _frame)
        return "".join([chunk async for chunk in response.body_iterator])

    frames = asyncio.run(collect())

    assert "event: done" in frames
    snapshot = runtime.history.dialogue_snapshot("c1")
    assert snapshot.state.active_topic.name == "糖尿病"
    assert snapshot.state.revision == 1


def _frame(event):
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

def test_switch_off_invalidates_the_state_and_re_enable_rebuilds(tmp_path):
    transport = DialogueTransport()
    service = AnswerService(transport)
    runtime = make_runtime(tmp_path, service)
    ask(runtime, "糖尿病在日常需要注意什么？", "r1")
    assert runtime.history.dialogue_snapshot("c1").state is not None

    # A turn answered while the switch is off has no observed semantics: the derived
    # state is marked for rebuild instead of being reused on re-enable.
    off = make_runtime(tmp_path, service, enabled=False)
    ask(off, "高血压在日常需要注意什么？", "r2")
    assert off.history.dialogue_snapshot("c1").state is None

    on = make_runtime(tmp_path, service)
    result = ask(on, "高血压有什么症状？", "r3")
    assert result["status"] == "success"
    rebuilt = on.history.dialogue_snapshot("c1").state
    assert rebuilt.active_topic.name == "高血压"
    assert rebuilt.revision == 1
