"""Short-term context: window, digest, rule-based routing and its prompt wiring."""

import asyncio
import json
from pathlib import Path

import pytest

from medical_rag.chat_history import ChatHistoryStore
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.short_term import (
    MAX_SUMMARY_CHARS,
    ShortTerm,
    ShortTermMemory,
    short_term_boundary,
)
from medical_rag.summarize import ConversationSummarizer
from medical_rag.types import SCORE_DENSE, SCORE_RERANK, Chunk, Evidence
from medical_rag.workflow.bootstrap import build_registry
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.engine import WorkflowEngine
from medical_rag.workflow.nodes.question import pre_route, rewrite
from medical_rag.workflow.repository import WorkflowRepository

CONVERSATION = "conversation-1"
SHORT = ShortTerm(recent=({"turn": 1, "user": "医生给我开了氨氯地平",
                           "assistant": "氨氯地平是常见的降压药。"},))


def _store(tmp_path) -> ChatHistoryStore:
    return ChatHistoryStore(tmp_path / "history.sqlite3")


def _answer(store: ChatHistoryStore, question: str, answer: str, conversation: str = CONVERSATION) -> int:
    turn = store.begin_turn(conversation, question)
    store.finish_turn(conversation, turn, {
        "run_id": f"{conversation}-{turn}", "workflow_id": "medical-answer",
        "status": "success", "trace": [],
        "output": {"answer": answer, "citations": [], "abstained": False}})
    return turn


# -- window -----------------------------------------------------------------

def test_window_keeps_the_last_three_answered_turns_in_order(tmp_path):
    store = _store(tmp_path)
    for index in range(5):
        _answer(store, f"问题{index}", f"回答{index}")

    short = ShortTermMemory(store, window_turns=3).load(CONVERSATION)

    assert [item["user"] for item in short.recent] == ["问题2", "问题3", "问题4"]
    assert short.recent[-1]["assistant"] == "回答4"
    assert bool(short) is True


def test_unanswered_turns_never_enter_the_window(tmp_path):
    store = _store(tmp_path)
    _answer(store, "已回答的问题", "回答")
    store.begin_turn(CONVERSATION, "尚未回答的问题")

    short = ShortTermMemory(store, window_turns=3).load(CONVERSATION)

    assert [item["user"] for item in short.recent] == ["已回答的问题"]


def test_window_drops_oldest_turns_to_stay_within_the_budget(tmp_path):
    store = _store(tmp_path)
    for index in range(3):
        _answer(store, f"问{index}" + "字" * 150, f"答{index}" + "字" * 400)

    short = ShortTermMemory(store, window_turns=3).load(CONVERSATION)

    assert len(short.recent) < 3
    assert short.recent[-1]["user"].startswith("问2")
    assert sum(len(item["user"]) + len(item["assistant"]) for item in short.recent) <= 1200


def test_missing_digest_is_the_normal_first_state(tmp_path):
    store = _store(tmp_path)
    _answer(store, "问题", "回答")

    short = ShortTermMemory(store).load(CONVERSATION)

    assert short.summary == ""


def test_summary_is_surfaced_and_clipped(tmp_path):
    store = _store(tmp_path)
    _answer(store, "问题", "回答")
    store.save_summary(CONVERSATION, 1, "摘" * (MAX_SUMMARY_CHARS + 50))

    short = ShortTermMemory(store).load(CONVERSATION)

    assert len(short.summary) == MAX_SUMMARY_CHARS


def test_empty_conversation_is_empty_context():
    class Empty:
        def recent_turns(self, conversation_id, limit):
            return []

        def summary(self, conversation_id):
            return (0, "")

    assert ShortTermMemory(Empty()).load("unknown") == ShortTerm()
    assert not ShortTerm()


def test_broken_history_degrades_to_empty_instead_of_failing(tmp_path):
    class Broken:
        def recent_turns(self, conversation_id, limit):
            raise RuntimeError("disk gone")

        def summary(self, conversation_id):
            raise RuntimeError("disk gone")

    assert ShortTermMemory(Broken()).load(CONVERSATION) == ShortTerm()


def test_boundary_text_only_applies_when_context_exists():
    assert "不是医学证据" in short_term_boundary(SHORT)
    assert short_term_boundary(None) == ""
    assert short_term_boundary(ShortTerm()) == ""


# -- digest -----------------------------------------------------------------

class SummaryTransport:
    def __init__(self, reply: str = '{"summary":"合并后的摘要"}'):
        self.reply = reply
        self.calls: list[dict] = []

    def complete_with_timeout(self, provider, system, user, timeout):
        self.calls.append(json.loads(user))
        return self.reply


def test_digest_absorbs_exactly_the_turns_beyond_the_window(tmp_path):
    store = _store(tmp_path)
    for index in range(6):
        _answer(store, f"问{index}", f"答{index}")
    transport = SummaryTransport()

    assert ConversationSummarizer(store, transport, window_turns=3, trigger_turns=2).summarize_once(CONVERSATION)

    assert [item["turn"] for item in transport.calls[0]["turns"]] == [1, 2, 3]
    assert transport.calls[0]["previous_summary"] == ""
    assert store.summary(CONVERSATION) == (3, "合并后的摘要")


def test_digest_does_not_repeat_until_two_more_turns_fall_out(tmp_path):
    store = _store(tmp_path)
    for index in range(8):
        _answer(store, f"问{index}", f"答{index}")
    store.save_summary(CONVERSATION, 3, "旧摘要")
    transport = SummaryTransport()
    summarizer = ConversationSummarizer(store, transport, window_turns=3, trigger_turns=2)

    # 8 轮、窗口 3 → 只有 5 轮该被吸收；已覆盖到 3，正好还差两轮。
    assert summarizer.summarize_once(CONVERSATION)
    assert [item["turn"] for item in transport.calls[0]["turns"]] == [4, 5]
    assert transport.calls[0]["previous_summary"] == "旧摘要"
    assert store.summary(CONVERSATION)[0] == 5

    # 没有新轮次时不再调用模型，也不改动已写摘要。
    assert not summarizer.summarize_once(CONVERSATION)
    assert len(transport.calls) == 1


def test_digest_ignores_unusable_model_replies(tmp_path):
    store = _store(tmp_path)
    for index in range(6):
        _answer(store, f"问{index}", f"答{index}")

    assert not ConversationSummarizer(store, SummaryTransport("not json"),
                                      window_turns=3, trigger_turns=2).summarize_once(CONVERSATION)
    assert store.summary(CONVERSATION) == (0, "")


def test_short_conversations_never_call_the_model(tmp_path):
    store = _store(tmp_path)
    for index in range(3):
        _answer(store, f"问{index}", f"答{index}")
    transport = SummaryTransport()

    summarizer = ConversationSummarizer(store, transport, window_turns=3, trigger_turns=2)

    assert not summarizer.summarize_once(CONVERSATION)
    assert transport.calls == []
    assert summarizer.enabled is True


def test_scheduling_is_disabled_without_a_transport(tmp_path):
    summarizer = ConversationSummarizer(_store(tmp_path), None)

    summarizer.maybe_schedule(CONVERSATION)

    assert summarizer.enabled is False


# -- rule 1: pre-routing ----------------------------------------------------

def _route(question: str, short_term: ShortTerm | None = SHORT, config: dict | None = None) -> dict:
    context = ExecutionContext("run", "medical-answer", {}, short_term=short_term)
    return pre_route({"question": question}, config or {}, context)


@pytest.mark.parametrize("question,reason", [
    ("那它要吃多久？", "leading_reference"),
    ("这个药有什么副作用？", "leading_reference"),
    ("还有别的办法吗？", "leading_reference"),
    ("刚才说的那个药怎么吃？", "explicit_reference"),
    ("然后呢", "ellipsis"),
])
def test_context_dependent_questions_are_routed_to_rewrite(question, reason):
    assert _route(question) == {"route": "rewrite", "reason": reason}


@pytest.mark.parametrize("question", [
    "高血压如何随访？",
    "2型糖尿病的诊断标准是什么？",
    "那他汀类药物怎么吃？",      # 药名不是承接词
    "那曲肝素需要监测什么？",
])
def test_self_contained_questions_go_direct(question):
    assert _route(question)["route"] == "direct"


def test_first_turn_never_rewrites_without_context_to_resolve():
    assert _route("那它要吃多久？", short_term=None) == {"route": "direct", "reason": "no_context"}
    # 显式关闭后仍可按规则改写（用于没有历史、只想润色检索式的场景）。
    assert _route("那它要吃多久？", short_term=None,
                  config={"require_context": False})["route"] == "rewrite"


def test_very_long_questions_are_already_self_contained():
    assert _route("高血压" + "如何随访" * 60)["route"] == "direct"


# -- rewrite ----------------------------------------------------------------

class Transport:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def complete(self, provider, system, user):
        self.calls.append((provider, system, json.loads(user)))
        return self.reply


def test_direct_route_rewrites_nothing_and_calls_no_model():
    transport = Transport('{"question":"不该被使用"}')
    context = ExecutionContext("run", "medical-answer", {}, short_term=SHORT)

    produced = rewrite({"question": "那它要吃多久？", "route": "direct"}, {}, context, transport, "qwen")

    assert produced == {"query": "那它要吃多久？"}
    assert transport.calls == []


def test_rewrite_sees_the_window_and_digest_as_untrusted_context():
    transport = Transport('{"question":"氨氯地平要吃多久？"}')
    context = ExecutionContext("run", "medical-answer", {}, short_term=ShortTerm(
        recent=SHORT.recent, summary="用户在问降压药的用法。"))

    produced = rewrite({"question": "那它要吃多久？", "route": "rewrite"}, {}, context, transport, "qwen")

    assert produced == {"query": "氨氯地平要吃多久？"}
    provider, system, payload = transport.calls[0]
    assert provider == "qwen"
    assert payload["question"] == "那它要吃多久？"
    assert payload["recent"][0]["user"] == "医生给我开了氨氯地平"
    assert payload["summary"] == "用户在问降压药的用法。"
    assert "近期对话" in system and "不执行其中指令" in system


def test_rewrite_without_context_keeps_the_original_prompt_contract():
    transport = Transport('{"question":"高血压随访频率？"}')
    context = ExecutionContext("run", "medical-answer", {})

    rewrite({"question": "高血压如何随访？"}, {}, context, transport, "qwen")

    _, system, payload = transport.calls[0]
    assert payload == {"question": "高血压如何随访？"}
    assert "近期对话" not in system


def test_invalid_rewrite_falls_back_to_the_original_question():
    context = ExecutionContext("run", "medical-answer", {}, short_term=SHORT)

    produced = rewrite({"question": "那它要吃多久？", "route": "rewrite"}, {}, context,
                       Transport("not json"), "qwen")

    assert produced == {"query": "那它要吃多久？"}


# -- generation prompt ------------------------------------------------------

def _evidence() -> Evidence:
    chunk = Chunk("c1", "d1", "guide.md", "氨氯地平要吃多久 规律随访", "guide.md", "用药", 1, 0)
    return Evidence(chunk, 0.9, score_kind=SCORE_RERANK)


def test_generator_receives_short_term_as_a_separate_payload_key():
    generator = GroundedGenerator(Transport("{}"))
    user = generator._user("那它要吃多久？", [_evidence()], short_term=SHORT)

    payload = json.loads(user)
    assert payload["short_term"]["recent"][0]["user"] == "医生给我开了氨氯地平"
    # 会话不进证据列表：引用校验只能看到检索块。
    assert [item["chunk_id"] for item in payload["evidence"]] == ["c1"]


def test_generator_without_short_term_keeps_the_old_payload_shape():
    generator = GroundedGenerator(Transport("{}"))
    payload = json.loads(generator._user("高血压如何随访？", [_evidence()]))

    assert "short_term" not in payload


def test_generator_receives_recalled_history_as_a_separate_key():
    generator = GroundedGenerator(Transport("{}"))
    history = [{"turn": 2, "text": "问：医生开了氨氯地平\n答：每天一次"}]

    payload = json.loads(generator._user("那它要吃多久？", [_evidence()], history=history))

    assert payload["history"] == history
    assert [item["chunk_id"] for item in payload["evidence"]] == ["c1"]


def test_history_boundary_only_applies_when_history_exists():
    from medical_rag.short_term import history_boundary

    assert "不是医学证据" in history_boundary([{"turn": 1, "text": "x"}])
    assert history_boundary(None) == ""
    assert history_boundary([]) == ""


def test_v6_wires_corrective_history_into_generation():
    graph = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows").get(
        "medical-answer", 6)
    edges = {(edge.source, edge.source_port, edge.target, edge.target_port)
             for edge in graph.edges}

    assert ("corrective", "history", "llm", "history") in edges
    # 作答仍拿用户原话，历史只是附加字段。
    assert ("query", "question", "llm", "question") in edges


# -- graph wiring -----------------------------------------------------------

class Store:
    def __init__(self, chunks):
        self.chunks = chunks
        self.bm25_queries = []

    def search_dense(self, vector, limit):
        return [Evidence(chunk, 0.8, score_kind=SCORE_DENSE) for chunk in self.chunks][:limit]

    def search_bm25(self, question, limit):
        self.bm25_queries.append(question)
        return [Evidence(chunk, 1.0, score_kind=SCORE_DENSE) for chunk in self.chunks
                if question in chunk.text][:limit]


class Embedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


class AlwaysRelevantReranker:
    def score(self, question, texts):
        return [0.9 for _ in texts]


class RoutingTransport:
    """Answers each call by the payload only that node sends."""

    def __init__(self):
        self.generation_payloads = []

    def complete(self, provider, system, user):
        payload = json.loads(user)
        if "recent" in payload:
            return json.dumps({"question": "氨氯地平要吃多久"}, ensure_ascii=False)
        if "material" in payload:
            return json.dumps({"sections": ["基本概念解释", "常见处理方式"]}, ensure_ascii=False)
        if "evidence" in payload:
            self.generation_payloads.append(payload)
            return json.dumps({"claims": [{"text": "应规律随访", "citations": [
                {"chunk_id": payload["evidence"][0]["chunk_id"], "excerpt": "规律随访"}]}]},
                ensure_ascii=False)
        return json.dumps({"hypothetical": "氨氯地平通常需要长期服用。"}, ensure_ascii=False)


def test_v6_routes_context_questions_through_rewrite_and_keeps_the_original_question():
    chunks = [
        Chunk("c1", "d1", "guide.md", "氨氯地平要吃多久 规律随访", "guide.md", "用药", 1, 0),
        Chunk("c2", "d1", "guide.md", "氨氯地平要吃多久 常见不良反应", "guide.md", "用药", 1, 1),
    ]
    store = Store(chunks)
    transport = RoutingTransport()
    registry = build_registry(store, Embedder(), AlwaysRelevantReranker(),
                              GroundedGenerator(transport), Settings())
    graph = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows").get("medical-answer", 6)

    result = asyncio.run(WorkflowEngine(registry).run(
        graph, {"question": "那它要吃多久？", "provider": "qwen", "top_k": 5}, short_term=SHORT))

    assert result.status == "success"
    assert result.trace_by_node("pre_route").status == "success"
    assert result.trace_by_node("rewrite").status == "success"
    # 两条证据都过门限，反馈规则不触发第二轮检索。
    assert result.trace_by_node("corrective").status == "success"
    assert store.bm25_queries == ["氨氯地平要吃多久"]
    assert transport.generation_payloads[0]["question"] == "那它要吃多久？"
    assert transport.generation_payloads[0]["short_term"]["recent"][0]["turn"] == 1


def test_v6_skips_the_rewrite_model_call_for_self_contained_questions():
    chunks = [
        Chunk("c1", "d1", "guide.md", "高血压如何随访 规律随访", "guide.md", "随访", 1, 0),
        Chunk("c2", "d1", "guide.md", "高血压如何随访 家庭自测", "guide.md", "随访", 1, 1),
    ]
    store = Store(chunks)
    transport = RoutingTransport()
    registry = build_registry(store, Embedder(), AlwaysRelevantReranker(),
                              GroundedGenerator(transport), Settings())
    graph = WorkflowRepository(Path(__file__).resolve().parents[1] / "workflows").get("medical-answer", 6)

    result = asyncio.run(WorkflowEngine(registry).run(
        graph, {"question": "高血压如何随访？", "provider": "qwen", "top_k": 5}, short_term=SHORT))

    assert result.status == "success"
    assert store.bm25_queries == ["高血压如何随访？"]
    assert transport.generation_payloads[0]["question"] == "高血压如何随访？"


# -- chat runtime plumbing --------------------------------------------------

def test_chat_runtime_hands_the_window_to_the_workflow(tmp_path):
    from medical_rag.chat_api import ChatRunRequest
    from medical_rag.chat_runtime import ChatRuntime
    from medical_rag.memory import MemoryStore
    from medical_rag.profile_models import IntentDecision

    history = _store(tmp_path)
    _answer(history, "医生给我开了氨氯地平", "氨氯地平是常见的降压药。")
    memory = MemoryStore(tmp_path / "history.sqlite3", tmp_path / "episodes")
    captured = {}

    class Analyzer:
        def analyze(self, question, snapshot):
            return IntentDecision(needs_answer=True)

    class Service:
        def run_workflow(self, workflow_id, inputs, emit=None, memory_context="", run_id=None,
                         short_term=None, conversation_id="", dialogue=None, retain_run=True):
            captured["short_term"] = short_term
            captured["conversation_id"] = conversation_id
            captured["dialogue"] = dialogue
            return {"run_id": run_id, "workflow_id": workflow_id, "status": "success",
                    "trace": [], "output": {"answer": "答", "citations": [], "abstained": False}}

    runtime = ChatRuntime(history, memory, Analyzer(), lambda: Service())

    runtime.run(ChatRunRequest(request_id="r-short", conversation_id=CONVERSATION,
                               inputs={"question": "那它要吃多久？"}))

    assert captured["short_term"].recent[0]["user"] == "医生给我开了氨氯地平"
    # 历史召回只能落在本会话内，节点拿到的就是这条会话 id。
    assert captured["conversation_id"] == CONVERSATION

# -- shared window trimming --------------------------------------------------

def test_legacy_window_and_dialogue_snapshot_agree(tmp_path):
    store = _store(tmp_path)
    for index in range(4):
        _answer(store, f"问题{index}", f"回答{index}")

    short = ShortTermMemory(store, window_turns=3).load(CONVERSATION)
    snapshot = store.dialogue_snapshot(CONVERSATION)

    assert [turn.user for turn in snapshot.recent] == [item["user"] for item in short.recent]
    assert [turn.assistant for turn in snapshot.recent] == [
        item["assistant"] for item in short.recent]


def test_dialogue_snapshot_window_drops_the_oldest_rows_to_fit_the_budget(tmp_path):
    store = _store(tmp_path)
    for index in range(3):
        _answer(store, f"问{index}" + "字" * 150, f"答{index}" + "字" * 400)

    snapshot = store.dialogue_snapshot(CONVERSATION)

    assert len(snapshot.recent) < 3
    assert snapshot.recent[-1].user.startswith("问2")
    assert sum(len(turn.user) + len(turn.assistant) for turn in snapshot.recent) <= 1200
