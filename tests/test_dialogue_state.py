"""SQLite consistency: atomic snapshot, state CAS, digest boundaries, deletion."""

import json

import pytest
from pydantic import ValidationError

from medical_rag.chat_history import ChatHistoryStore, ChatMessage, Conversation
from medical_rag.dialogue_models import (
    ConversationState,
    DialogueRun,
    Resolution,
    Topic,
)
from medical_rag.dialogue_state import advance_state, can_commit_semantics
from medical_rag.summarize import ConversationSummarizer
from medical_rag.types import ApiError


def answered(store, conversation, question, answer="合成回答"):
    turn = store.begin_turn(conversation, question)
    store.finish_turn(conversation, turn, {
        "run_id": f"{conversation}-{turn}", "status": "success", "trace": [],
        "output": {"answer": answer, "citations": [], "abstained": False},
    })
    return turn


def resolved(question, full, name, source="current", intent="daily_precautions",
             text="日常生活注意事项", relation="standalone", answer_type=None):
    return Resolution.model_validate_json(json.dumps({
        "original_question": question, "status": "resolved", "relation": relation,
        "resolved_question": full, "topic": {"name": name, "type": "disease",
                                             "source_turn_id": source, "source_role": "user"},
        "intent": intent, "intent_text": text, "references": [], "clarification": None,
    }, ensure_ascii=False))


def clarification(question, candidates, intent="etiology", text="由什么引起"):
    return Resolution.model_validate_json(json.dumps({
        "original_question": question, "status": "clarification", "relation": "follow_up",
        "resolved_question": None, "topic": None, "intent": intent, "intent_text": text,
        "references": [],
        "clarification": {
            "candidates": [{"name": name, "type": "disease", "source_turn_id": source,
                            "source_role": "user"} for name, source in candidates],
            "intent": intent, "intent_text": text,
            "original_question": question, "source_turn_id": "current"},
    }, ensure_ascii=False))


def result(run_id, answer="合成回答", answer_type=None, status="success"):
    output = {"answer": answer, "citations": [], "abstained": False}
    if answer_type:
        output["answer_type"] = answer_type
    return {"run_id": run_id, "status": status, "trace": [], "output": output}


def test_digest_boundary_uses_conversation_positions(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    own = []
    for index in range(5):
        own.append(answered(store, "a", f"本会话问题{index}"))
        for other in range(4):
            answered(store, "b", f"其他会话{index}-{other}")
    candidates = store.digest_candidates("a", 0, 3)
    assert [row["turn"] for row in candidates] == own[:2]
    snapshot = store.dialogue_snapshot("a")
    assert snapshot.recent[-1].turn_id == str(own[-1])
    # Legacy stored answers have no answerType and must keep reading as an answer.
    assert snapshot.recent[-1].answer_type == "answer"


def test_digest_never_claims_a_turn_before_the_covered_boundary(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    for index in range(6):
        answered(store, "a", f"问题{index}")
    store.save_summary("a", 2, "旧摘要")
    assert [row["turn"] for row in store.digest_candidates("a", 2, 3)] == [3]


def test_state_commits_and_reloads_in_a_new_store_instance(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = ChatHistoryStore(path)
    turn = store.begin_turn("c", "糖尿病呢？")
    snapshot = store.dialogue_snapshot("c")
    run = DialogueRun(snapshot, resolved("糖尿病呢？", "糖尿病在日常需要注意什么？", "糖尿病"))
    store.finish_turn("c", turn, result("r1"), dialogue=run)

    reloaded = ChatHistoryStore(path).dialogue_snapshot("c")

    assert reloaded.state.active_topic.name == "糖尿病"
    assert reloaded.state.active_topic.source_turn_id == str(turn)
    assert reloaded.state.revision == 1
    assert reloaded.recent[-1].turn_id == str(turn)
    assert run.snapshot.state == reloaded.state


def test_clarification_is_stored_with_its_type_and_pending_item(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    answered(store, "c", "高血压呢？")
    answered(store, "c", "糖尿病呢？")
    turn = store.begin_turn("c", "它是什么引起的？")
    snapshot = store.dialogue_snapshot("c")
    pending = clarification("它是什么引起的？", [("高血压", "1"), ("糖尿病", "2")])
    store.finish_turn("c", turn, result("r1", "你指的是 高血压，还是 糖尿病？",
                                        answer_type="clarification"),
                      dialogue=DialogueRun(snapshot, pending))

    reloaded = store.dialogue_snapshot("c")
    assert reloaded.recent[-1].answer_type == "clarification"
    assert [item.name for item in reloaded.state.pending_clarification.candidates] == [
        "高血压", "糖尿病"]
    assert reloaded.state.pending_clarification.intent == "etiology"


def test_technical_failure_does_not_advance_the_topic(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    first = store.begin_turn("c", "糖尿病呢？")
    store.finish_turn("c", first, result("r1"), dialogue=DialogueRun(
        store.dialogue_snapshot("c"), resolved("糖尿病呢？", "糖尿病在日常需要注意什么？", "糖尿病")))
    before = store.dialogue_snapshot("c")

    failed = store.begin_turn("c", "它要怎么治疗？")
    store.finish_turn("c", failed, result("r2", "", status="failed"),
                      dialogue=DialogueRun(store.dialogue_snapshot("c")))

    after = store.dialogue_snapshot("c")
    assert after.state.active_topic.name == "糖尿病"
    assert after.state.revision == before.state.revision
    assert after.context_version > before.context_version


def test_stale_snapshot_is_rejected_and_the_answer_is_not_saved(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    first = store.begin_turn("c", "糖尿病呢？")
    stale = store.dialogue_snapshot("c")
    store.finish_turn("c", first, result("r1"), dialogue=DialogueRun(
        stale, resolved("糖尿病呢？", "糖尿病在日常需要注意什么？", "糖尿病")))

    turn = store.begin_turn("c", "那它由什么引起？")
    run = DialogueRun(stale, resolved("那它由什么引起？", "糖尿病是由什么引起的？", "糖尿病",
                                      source="1", intent="etiology", relation="follow_up"))
    with pytest.raises(ApiError) as caught:
        store.finish_turn("c", turn, result("r2"), dialogue=run)
    assert caught.value.code == "CONVERSATION_CONTEXT_CHANGED"
    saved = store.get_conversation("c")["messages"]
    assert [item["content"] for item in saved] == ["糖尿病呢？", "合成回答", "那它由什么引起？"]


def test_oversized_state_rolls_back_the_whole_answer(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    store.begin_turn("c", "第一个完整问题？")
    topic = Topic(name="糖" * 100, type="disease", source_turn_id="1", source_role="user")
    previous = ConversationState(
        revision=1, through_turn_id="1", active_topic=topic,
        last_resolved_question="问" * 1000)
    turn = store.begin_turn("c", "第二个问题")
    candidates = [{"name": f"{index}" + "尿" * 99, "type": "disease",
                   "source_turn_id": "2" * 64, "source_role": "user"} for index in range(3)]
    resolution = Resolution.model_validate_json(json.dumps({
        "original_question": "第二个问题", "status": "clarification",
        "relation": "follow_up", "resolved_question": None, "topic": None,
        "intent": "etiology", "intent_text": "意" * 200, "references": [],
        "clarification": {"candidates": candidates, "intent": "etiology",
                          "intent_text": "意" * 200, "original_question": "原" * 1000,
                          "source_turn_id": "current"},
    }, ensure_ascii=False))
    snapshot = store.dialogue_snapshot("c").model_copy(update={"state": previous})
    with pytest.raises(ValidationError):
        store.finish_turn("c", turn, result("r2"),
                          dialogue=DialogueRun(snapshot, resolution))
    saved = store.get_conversation("c")["messages"]
    assert saved[-1]["content"] == "第二个问题"


def test_context_versions_are_monotonic_across_content_paths(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    answered(store, "c", "问题一")
    first = store.dialogue_snapshot("c").context_version
    answered(store, "c", "问题二")
    assert store.dialogue_snapshot("c").context_version > first


def test_two_distinct_requests_for_one_conversation_are_mutually_exclusive(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    store.begin_request("c", "r1", "问题一", "fp1")
    with pytest.raises(ApiError) as caught:
        store.begin_request("c", "r2", "问题二", "fp2")
    assert caught.value.code == "REQUEST_IN_PROGRESS"
    # Another conversation is independent.
    assert store.begin_request("d", "r3", "问题三", "fp3")["state"] == "new"


def test_same_request_replay_does_not_add_a_second_turn(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    first = store.begin_request("c", "r1", "问题一", "fp1")
    with pytest.raises(ApiError) as caught:
        store.begin_request("c", "r1", "问题一", "fp1")
    assert caught.value.code == "REQUEST_IN_PROGRESS"

    store.release_request("r1", first["owner_token"])
    replayed = store.begin_request("c", "r1", "问题一", "fp1")

    assert replayed["state"] == "recoverable"
    assert replayed["turn_id"] == first["turn_id"]
    assert len(store.get_conversation("c")["messages"]) == 1


def test_delete_removes_the_derived_state(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    turn = store.begin_turn("c", "糖尿病呢？")
    store.finish_turn("c", turn, result("r1"), dialogue=DialogueRun(
        store.dialogue_snapshot("c"), resolved("糖尿病呢？", "糖尿病在日常需要注意什么？", "糖尿病")))

    store.delete_conversation("c")

    snapshot = store.dialogue_snapshot("c")
    assert snapshot.state is None
    assert snapshot.recent == ()
    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM conversation_states WHERE conversation_id='c'").fetchone()[0] == 0


def test_import_marks_the_content_version_but_keeps_import_insert_only(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    item = Conversation(id="legacy", title="旧会话", messages=[
        ChatMessage(role="user", content="高血压呢？")])
    store.import_conversations([item])
    first = store.dialogue_snapshot("legacy").context_version
    store.import_conversations([item])
    assert store.dialogue_snapshot("legacy").context_version == first
    assert first >= 1


def test_invalid_persisted_state_degrades_to_the_authorised_window(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    turn = store.begin_turn("c", "糖尿病呢？")
    store.finish_turn("c", turn, result("r1"), dialogue=DialogueRun(
        store.dialogue_snapshot("c"), resolved("糖尿病呢？", "糖尿病在日常需要注意什么？", "糖尿病")))
    with store._connect() as connection:
        connection.execute(
            "UPDATE conversation_states SET state_json=? WHERE conversation_id='c'",
            (json.dumps({"schema_version": 1, "revision": 1, "active_topic": {
                "name": "伪造对象", "type": "disease", "source_turn_id": str(turn),
                "source_role": "user"}}, ensure_ascii=False),))

    snapshot = store.dialogue_snapshot("c")

    assert snapshot.state is None
    assert snapshot.recent[-1].user == "糖尿病呢？"


def test_summarizer_absorbs_the_earliest_uncovered_turns(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    for index in range(8):
        answered(store, "a", f"问{index}")
        answered(store, "b", f"干扰{index}")

    class Transport:
        def __init__(self):
            self.payloads = []

        def complete_with_timeout(self, provider, system, user, timeout):
            self.payloads.append(json.loads(user))
            return '{"summary":"合并摘要"}'

    transport = Transport()
    assert ConversationSummarizer(store, transport, window_turns=3, trigger_turns=2).summarize_once("a")
    # IDs are global: conversation "a" owns 1,3,5,7,9 while "b" consumes the rest.
    assert [turn["turn"] for turn in transport.payloads[0]["turns"]] == [1, 3, 5, 7, 9]
    assert store.summary("a") == (9, "合并摘要")


def test_summarizer_does_not_skip_an_earlier_uncovered_turn(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    for index in range(12):
        answered(store, "a", f"问{index}")
    store.save_summary("a", 2, "旧摘要")
    payloads = []

    class Transport:
        def complete_with_timeout(self, provider, system, user, timeout):
            payloads.append(json.loads(user))
            return '{"summary":"新摘要"}'

    assert ConversationSummarizer(store, Transport(), window_turns=3,
                                  trigger_turns=2).summarize_once("a")
    assert [turn["turn"] for turn in payloads[0]["turns"]] == [3, 4, 5, 6, 7, 8, 9]
    assert store.summary("a")[0] == 9


@pytest.mark.parametrize("status,output,expected", [
    ("success", {"answer": "有答案", "abstained": False}, True),
    ("success", {"answer": "", "abstained": False}, False),
    ("success", {"answer": "资料不足", "abstained": True,
                 "abstain_reason": "ABSTAIN_NO_EVIDENCE"}, True),
    ("success", {"answer": "资料不足", "abstained": True,
                 "abstain_reason": "ABSTAIN_UNANCHORED"}, True),
    ("success", {"answer": "生成异常", "abstained": True,
                 "abstain_reason": "ABSTAIN_PARSE_ERROR"}, False),
    ("success", {"answer": "被截断", "abstained": True,
                 "abstain_reason": "ABSTAIN_TRUNCATED"}, False),
    ("success", {"answer": "请补充对象", "abstained": False,
                 "answer_type": "clarification"}, True),
    ("failed", {"answer": "错误", "abstained": False}, False),
])
def test_semantic_commit_whitelist(status, output, expected):
    assert can_commit_semantics(status, output) is expected


def test_advance_state_is_pure_for_the_input_state():
    state = ConversationState(revision=1, through_turn_id="1")
    before = state.model_dump_json()
    advance_state(state, resolved("糖尿病呢？", "糖尿病在日常需要注意什么？", "糖尿病"),
                  turn_id="2")
    assert state.model_dump_json() == before
