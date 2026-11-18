"""Strict resolution contract: bounded calls, source checks, clarifications."""

import json

import pytest
from pydantic import ValidationError

from medical_rag.dialogue_models import (
    ConversationState,
    DialogueSnapshot,
    PendingClarification,
    RecentTurn,
    Resolution,
    ResolverReply,
    Topic,
)
from medical_rag.dialogue_state import advance_state
from medical_rag.question_resolver import (
    GENERIC_CLARIFICATION,
    RESOLUTION_TIMEOUT_SECONDS,
    QuestionResolver,
    clarification_answer,
    validate_resolution_sources,
)


class SequenceTransport:
    """Record every resolution call and replay one reply per call."""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def complete_with_timeout(self, provider, system, user, timeout):
        self.calls.append((provider, system, json.loads(user), timeout))
        return json.dumps(next(self.replies), ensure_ascii=False)


def reply(topic, source, intent, text, question, relation="standalone",
          references=(), clarification=None, status="resolved", role="user"):
    return {
        "status": status, "relation": relation,
        "resolved_question": question, "intent": intent, "intent_text": text,
        "topic": ({"name": topic, "type": "disease",
                   "source_turn_id": source, "source_role": role} if topic else None),
        "references": list(references), "clarification": clarification,
    }


def topic(name, source="current", kind="disease", role="user"):
    return {"name": name, "type": kind, "source_turn_id": source, "source_role": role}


def test_three_turn_topic_switch_and_anaphora():
    questions = ["高血压在日常生活中需要注意什么？", "糖尿病呢？", "那它是由什么引起的？"]
    expected = [questions[0], "糖尿病在日常生活中需要注意什么？", "糖尿病是由什么引起的？"]
    transport = SequenceTransport([
        reply("高血压", "current", "daily_precautions", "日常生活注意事项", expected[0]),
        reply("糖尿病", "current", "daily_precautions", "日常生活注意事项", expected[1],
              "topic_switch"),
        reply("糖尿病", "2", "etiology", "由什么引起", expected[2], "follow_up"),
    ])
    resolver = QuestionResolver(transport)
    snapshot = DialogueSnapshot()
    state = None
    for index, (question, full) in enumerate(zip(questions, expected), 1):
        result = resolver.resolve(question, snapshot)
        assert result.original_question == question
        assert result.resolved_question == full
        state = advance_state(snapshot.state, result, turn_id=str(index))
        snapshot = DialogueSnapshot(
            state=state,
            recent=snapshot.recent + (
                RecentTurn(turn_id=str(index), user=question, assistant="合成回答"),),
            context_version=index,
        )
    assert state.active_topic.name == "糖尿病"
    assert state.active_intent == "etiology"
    assert state.revision == 3
    assert all(call[3] == RESOLUTION_TIMEOUT_SECONDS for call in transport.calls)
    assert len(transport.calls) == 3


def test_resolution_keeps_the_original_question_out_of_the_model_contract():
    with pytest.raises(ValidationError):
        ResolverReply.model_validate_json(json.dumps(
            {**reply("糖尿病", "current", "etiology", "病因", "糖尿病病因？"),
             "original_question": "被伪造的原问题"}, ensure_ascii=False))


def test_unknown_fields_are_rejected_and_degrade_to_clarification():
    transport = SequenceTransport([{**reply("糖尿病", "current", "etiology", "病因",
                                            "糖尿病病因？"), "confidence": 0.9}])
    result = QuestionResolver(transport).resolve("糖尿病病因？", DialogueSnapshot())
    assert result.status == "clarification"
    assert result.resolved_question is None


def test_clarification_cannot_carry_an_executable_question():
    with pytest.raises(ValidationError):
        ResolverReply.model_validate_json(json.dumps(
            reply("糖尿病", "current", "etiology", "病因", "糖尿病病因？",
                  status="clarification"), ensure_ascii=False))


def test_resolved_requires_a_complete_question():
    with pytest.raises(ValidationError):
        ResolverReply.model_validate_json(json.dumps(
            {"status": "resolved", "relation": "follow_up", "resolved_question": None,
             "intent": "other"}, ensure_ascii=False))


def test_no_context_pronoun_skips_the_model_and_asks_for_the_object():
    transport = SequenceTransport([])
    result = QuestionResolver(transport).resolve("它是什么引起的？", DialogueSnapshot())
    assert result.status == "clarification"
    assert transport.calls == []
    assert clarification_answer(result)["answer"] == GENERIC_CLARIFICATION


def test_pronoun_with_history_does_call_the_model():
    transport = SequenceTransport([reply("糖尿病", "1", "etiology", "由什么引起",
                                         "糖尿病是由什么引起的？", "follow_up")])
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答"),))
    result = QuestionResolver(transport).resolve("那它是由什么引起的？", snapshot)
    assert result.resolved_question == "糖尿病是由什么引起的？"
    assert len(transport.calls) == 1


def test_ambiguity_returns_sourced_candidates_and_a_fixed_template():
    clarification = {
        "candidates": [topic("高血压", "1"), topic("糖尿病", "2")],
        "intent": "etiology", "intent_text": "由什么引起",
        "original_question": "它是什么引起的？", "source_turn_id": "current",
    }
    transport = SequenceTransport([reply(None, "current", "etiology", "由什么引起", None,
                                         "follow_up", clarification=clarification,
                                         status="clarification")])
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="高血压是什么？", assistant="合成回答"),
        RecentTurn(turn_id="2", user="糖尿病呢？", assistant="合成回答"),
    ))
    result = QuestionResolver(transport).resolve("它是什么引起的？", snapshot)
    assert result.status == "clarification"
    assert [item.name for item in result.clarification.candidates] == ["高血压", "糖尿病"]
    assert clarification_answer(result)["answer"] == "你指的是 高血压，还是 糖尿病？"
    assert clarification_answer(result)["citations"] == []
    assert clarification_answer(result)["answer_type"] == "clarification"


def test_pending_clarification_is_restored_on_a_short_answer():
    pending = PendingClarification(
        candidates=(Topic(name="高血压", type="disease", source_turn_id="1", source_role="user"),
                    Topic(name="糖尿病", type="disease", source_turn_id="2", source_role="user")),
        intent="etiology", intent_text="由什么引起", source_turn_id="3")
    state = ConversationState(revision=3, through_turn_id="3", pending_clarification=pending)
    transport = SequenceTransport([
        reply("糖尿病", "2", "etiology", "由什么引起", "糖尿病是由什么引起的？",
              "clarification_reply"),
    ])
    snapshot = DialogueSnapshot(
        state=state,
        recent=(RecentTurn(turn_id="2", user="糖尿病呢？", assistant="合成回答"),
                RecentTurn(turn_id="3", user="它是什么引起的？", assistant="你指的是高血压，还是糖尿病？")),
        context_version=3)
    result = QuestionResolver(transport).resolve("糖尿病", snapshot)
    assert result.status == "resolved"
    advanced = advance_state(state, result, turn_id="4")
    assert advanced.pending_clarification is None
    assert advanced.active_intent == "etiology"
    assert advanced.active_topic.source_turn_id == "2"


def test_fabricated_source_is_rejected():
    transport = SequenceTransport([reply("糖尿病", "999", "etiology", "病因", "糖尿病病因？")])
    result = QuestionResolver(transport).resolve("糖尿病病因？", DialogueSnapshot())
    assert result.status == "clarification"


def test_wrong_role_for_the_same_turn_is_rejected():
    transport = SequenceTransport([reply("糖尿病", "1", "etiology", "病因", "糖尿病病因？",
                                         role="assistant")])
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答"),))
    result = QuestionResolver(transport).resolve("糖尿病病因？", snapshot)
    assert result.status == "clarification"


def test_reference_must_appear_in_the_question_and_in_its_source():
    good = SequenceTransport([reply(
        "糖尿病", "1", "etiology", "病因", "糖尿病病因？",
        references=[{"expression": "它", "entity": "糖尿病", "source_turn_id": "1"}])])
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答"),))
    assert QuestionResolver(good).resolve("它是什么引起的？", snapshot).status == "resolved"

    invented = SequenceTransport([reply(
        "糖尿病", "1", "etiology", "病因", "糖尿病病因？",
        references=[{"expression": "它", "entity": "高血压", "source_turn_id": "1"}])])
    assert QuestionResolver(invented).resolve(
        "它是什么引起的？", snapshot).status == "clarification"


def test_reference_role_pins_the_entity_to_that_speaker():
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答提到骨质疏松"),))

    user_role = SequenceTransport([reply(
        "糖尿病", "1", "etiology", "病因", "糖尿病病因？",
        references=[{"expression": "它", "entity": "糖尿病", "source_turn_id": "1",
                     "source_role": "user"}])])
    assert QuestionResolver(user_role).resolve("它是什么引起的？", snapshot).status == "resolved"

    # 实体出自助手文本：topic 与 reference 都必须按 assistant 标注。
    assistant_role = SequenceTransport([reply(
        "骨质疏松", "1", "etiology", "病因", "骨质疏松病因？", role="assistant",
        references=[{"expression": "它", "entity": "骨质疏松", "source_turn_id": "1",
                     "source_role": "assistant"}])])
    assert QuestionResolver(assistant_role).resolve(
        "它是什么引起的？", snapshot).status == "resolved"


def test_reference_role_claims_are_validated_like_topics():
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答提到骨质疏松"),))

    # topic 合法（assistant 提到骨质疏松），失败必须归因于 reference：
    # 实体只在助手文本里，却声称 user 说过——与 topic 的角色错配同样拒绝。
    wrong_role = SequenceTransport([reply(
        "骨质疏松", "1", "etiology", "病因", "骨质疏松病因？", role="assistant",
        references=[{"expression": "它", "entity": "骨质疏松", "source_turn_id": "1",
                     "source_role": "user"}])])
    assert QuestionResolver(wrong_role).resolve(
        "它是什么引起的？", snapshot).status == "clarification"

    # 声称 assistant 说过糖尿病，但助手文本里没有：钉死检查同样拒绝。
    wrong_speaker = SequenceTransport([reply(
        "糖尿病", "1", "etiology", "病因", "糖尿病病因？",
        references=[{"expression": "它", "entity": "糖尿病", "source_turn_id": "1",
                     "source_role": "assistant"}])])
    assert QuestionResolver(wrong_speaker).resolve(
        "它是什么引起的？", snapshot).status == "clarification"

    # 声明了快照里不存在的 (turn_id, role) 对，等同于伪造来源。
    unknown_pair = SequenceTransport([reply(
        "糖尿病", "1", "etiology", "病因", "糖尿病病因？",
        references=[{"expression": "它", "entity": "糖尿病", "source_turn_id": "9",
                     "source_role": "user"}])])
    assert QuestionResolver(unknown_pair).resolve(
        "它是什么引起的？", snapshot).status == "clarification"


def test_model_emitting_reference_role_still_resolves():
    """回归：qwen 思考模式曾把 source_role 写进 references，旧契约整条拒绝。"""
    transport = SequenceTransport([reply(
        "糖尿病", "2", "etiology", "由什么引起", "糖尿病是由什么引起的？", "follow_up",
        references=[{"expression": "它", "entity": "糖尿病", "source_turn_id": "2",
                     "source_role": "user"}])])
    state = ConversationState(
        revision=2, through_turn_id="2",
        active_topic=Topic(name="糖尿病", type="disease", source_turn_id="2",
                           source_role="user"))
    snapshot = DialogueSnapshot(
        state=state,
        recent=(RecentTurn(turn_id="2", user="糖尿病呢？", assistant="合成回答"),),
        context_version=2)
    result = QuestionResolver(transport).resolve("那它是由什么引起的？", snapshot)
    assert result.status == "resolved"
    assert result.resolved_question == "糖尿病是由什么引起的？"
    assert result.references[0].source_role == "user"


def test_changed_numeric_constraint_is_rejected():
    transport = SequenceTransport([reply("高血压", "current", "treatment", "治疗",
                                         "高血压每天服用5mg如何治疗？")])
    result = QuestionResolver(transport).resolve("高血压如何治疗？", DialogueSnapshot())
    assert result.status == "clarification"


def test_empty_topic_with_a_complete_question_is_accepted():
    transport = SequenceTransport([reply(None, "current", "other", "", "高血压和糖尿病如何比较？")])
    result = QuestionResolver(transport).resolve("高血压和糖尿病如何比较？", DialogueSnapshot())
    assert result.status == "resolved"


def test_resolved_question_must_keep_its_topic():
    transport = SequenceTransport([reply("糖尿病", "current", "etiology", "病因", "高血压病因？")])
    assert QuestionResolver(transport).resolve(
        "糖尿病病因？", DialogueSnapshot()).status == "clarification"


def test_oversized_or_non_string_model_output_degrades():
    class Big:
        def complete_with_timeout(self, provider, system, user, timeout):
            return "x" * 40000

    assert QuestionResolver(Big()).resolve(
        "糖尿病病因？", DialogueSnapshot()).status == "clarification"

    class NotText:
        def complete_with_timeout(self, provider, system, user, timeout):
            return {"status": "resolved"}

    assert QuestionResolver(NotText()).resolve(
        "糖尿病病因？", DialogueSnapshot()).status == "clarification"


def test_transport_failure_and_timeout_never_raise():
    class Failing:
        def complete_with_timeout(self, provider, system, user, timeout):
            raise TimeoutError("model timeout")

    result = QuestionResolver(Failing()).resolve("糖尿病病因？", DialogueSnapshot())
    assert result.status == "clarification"
    assert result.relation == "follow_up"
    assert result.topic is None


def test_long_question_is_not_clipped_to_the_old_rewrite_limit():
    question = "高血压" + "如何随访" * 2000
    transport = SequenceTransport([reply("高血压", "current", "treatment", "治疗", question)])
    result = QuestionResolver(transport).resolve(question, DialogueSnapshot())
    assert result.status == "resolved"
    assert result.resolved_question == question


def test_invalid_questions_are_rejected_before_any_call():
    transport = SequenceTransport([])
    resolver = QuestionResolver(transport)
    for value in ("", "   ", "问" * 20001, None):
        with pytest.raises(ValueError):
            resolver.resolve(value, DialogueSnapshot())
    assert transport.calls == []


def test_snapshot_rejects_oversized_window_and_turn():
    with pytest.raises(ValidationError):
        DialogueSnapshot(recent=(RecentTurn(turn_id="1", user="问" * 201),))
    with pytest.raises(ValidationError):
        DialogueSnapshot(recent=tuple(
            RecentTurn(turn_id=str(index), user="问" * 200, assistant="答" * 400)
            for index in range(4)))
    with pytest.raises(ValidationError):
        DialogueSnapshot(recent=(
            RecentTurn(turn_id="1", user="问" * 200, assistant="答" * 400),
            RecentTurn(turn_id="2", user="问" * 200, assistant="答" * 400),
            RecentTurn(turn_id="3", user="问" * 200, assistant="答" * 400)))


def test_state_budget_rejects_oversized_json_instead_of_truncating():
    long_topic = Topic(name="糖" * 100, type="disease", source_turn_id="1", source_role="user")
    candidates = tuple(
        Topic(name=f"{index}" + "尿" * 99, type="disease", source_turn_id="2" * 64,
              source_role="user")
        for index in range(3))
    with pytest.raises(ValidationError):
        ConversationState(
            revision=1, through_turn_id="9" * 128, active_topic=long_topic,
            active_intent_text="意" * 200, last_resolved_question="问" * 1000,
            pending_clarification=PendingClarification(
                candidates=candidates, intent_text="意" * 200,
                original_question="原" * 1000, source_turn_id="8" * 128))


def test_source_validation_accepts_a_state_topic_outside_the_window():
    state = ConversationState(
        revision=2, through_turn_id="2",
        active_topic=Topic(name="糖尿病", type="disease", source_turn_id="1",
                           source_role="user"))
    snapshot = DialogueSnapshot(state=state)
    decoded = ResolverReply.model_validate_json(json.dumps(
        reply("糖尿病", "1", "symptoms", "症状", "糖尿病有什么症状？", "follow_up"),
        ensure_ascii=False))
    validate_resolution_sources(decoded, "它有什么症状？", snapshot)


def test_source_validation_rejects_a_turn_that_does_not_mention_the_entity():
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="高血压呢？", assistant="合成回答"),))
    decoded = ResolverReply.model_validate_json(json.dumps(
        reply("糖尿病", "1", "symptoms", "症状", "糖尿病有什么症状？"),
        ensure_ascii=False))
    with pytest.raises(ValueError):
        validate_resolution_sources(decoded, "糖尿病有什么症状？", snapshot)


def test_state_advance_remaps_only_the_current_source():
    resolution = Resolution.model_validate_json(json.dumps(
        {**reply("糖尿病", "current", "etiology", "病因", "糖尿病病因？"),
         "original_question": "糖尿病病因？"}, ensure_ascii=False))
    state = advance_state(None, resolution, turn_id="7")
    assert state.active_topic.source_turn_id == "7"
    assert state.revision == 1
    assert state.through_turn_id == "7"

    older = Resolution.model_validate_json(json.dumps(
        {**reply("糖尿病", "1", "etiology", "病因", "糖尿病病因？", "follow_up"),
         "original_question": "那它是什么引起的？"}, ensure_ascii=False))
    assert advance_state(state, older, turn_id="8").active_topic.source_turn_id == "1"


def test_oversized_resolved_question_is_not_copied_into_the_state():
    full = "糖尿病" + "如何随访" * 400
    resolution = Resolution.model_validate_json(json.dumps(
        {**reply("糖尿病", "current", "treatment", "治疗", full),
         "original_question": "糖尿病如何随访？"}, ensure_ascii=False))
    state = advance_state(None, resolution, turn_id="1")
    assert state.last_resolved_question == ""
    assert state.active_topic.name == "糖尿病"


def test_failed_clarification_keeps_an_earlier_pending_item():
    pending = PendingClarification(
        candidates=(Topic(name="高血压", type="disease", source_turn_id="1", source_role="user"),),
        intent="etiology", intent_text="由什么引起", source_turn_id="2")
    state = ConversationState(revision=2, through_turn_id="2", pending_clarification=pending)
    fallback = Resolution(original_question="它呢", status="clarification", relation="follow_up")
    advanced = advance_state(state, fallback, turn_id="3")
    assert advanced.pending_clarification == pending
    assert advanced.revision == 3



def test_resolver_parses_fenced_json_and_folded_topic():
    from medical_rag.question_resolver import is_self_contained_question

    assert is_self_contained_question("它是什么引起的？", DialogueSnapshot()) is False
    assert is_self_contained_question("高血压和糖尿病，它是什么引起的？", DialogueSnapshot()) is False
    assert is_self_contained_question("CT和MRI哪个好？", DialogueSnapshot()) is False
    assert is_self_contained_question("饮食和运动哪个更重要？", DialogueSnapshot()) is False
    assert is_self_contained_question("糖尿病", DialogueSnapshot()) is False
    assert is_self_contained_question(
        "临床上诊断2型糖尿病（T2DM）的主要血糖阈值标准与糖化血红蛋白切点有哪些具体要求？",
        DialogueSnapshot(),
    ) is True

    fenced = (
        "```json\n"
        + json.dumps(
            reply(
                "2 型糖尿病",
                "current",
                "medication_usage",
                "如何使用",
                "2型糖尿病患者如何使用胰岛素治疗？",
            ),
            ensure_ascii=False,
        )
        + "\n```"
    )
    class RawTransport:
        def complete_with_timeout(self, provider, system, user, timeout):
            return fenced

    resolved = QuestionResolver(RawTransport()).resolve(
        "2型糖尿病（患者）如何使用胰岛素治疗？", DialogueSnapshot()
    )
    assert resolved.status == "resolved"
    assert resolved.topic is not None
    assert resolved.topic.name == "2 型糖尿病"


def test_resolver_passes_json_mode_when_supported():
    calls = []

    class JsonAwareTransport:
        def complete_with_timeout(self, provider, system, user, timeout,
                                  *, json_mode=False, max_tokens=None):
            calls.append((provider, timeout, json_mode, max_tokens))
            return json.dumps(
                reply("高血压", "current", "etiology", "由什么引起", "高血压由什么引起？"),
                ensure_ascii=False,
            )

    resolved = QuestionResolver(JsonAwareTransport()).resolve("高血压由什么引起？", DialogueSnapshot())
    assert resolved.status == "resolved"
    assert calls == [("qwen", RESOLUTION_TIMEOUT_SECONDS, True, 1024)]
