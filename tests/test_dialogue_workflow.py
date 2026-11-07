"""v7 graph integration: one semantic question, early clarification, no leakage."""

import asyncio
import json
from pathlib import Path

from test_question_resolver import reply
from test_short_term import AlwaysRelevantReranker, Embedder, RoutingTransport, Store

from medical_rag.config import Settings
from medical_rag.dialogue_models import (
    ConversationState,
    DialogueRun,
    DialogueSnapshot,
    RecentTurn,
    Topic,
)
from medical_rag.generation import GroundedGenerator
from medical_rag.types import Chunk
from medical_rag.workflow.bootstrap import build_registry
from medical_rag.workflow.engine import WorkflowEngine
from medical_rag.workflow.repository import WorkflowRepository

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
FULL = "糖尿病是由什么引起的？"
FOLLOW_UP = "那它是由什么引起的？"


class DialogueTransport(RoutingTransport):
    """Answers the resolver call and delegates every other node to the v6 fake."""

    def __init__(self, topic="糖尿病", source="current", full=FULL, intent="etiology",
                 intent_text="由什么引起", relation="standalone"):
        super().__init__()
        self.payloads = []
        self.answer = reply(topic, source, intent, intent_text, full, relation)

    def complete(self, provider, system, user):
        payload = json.loads(user)
        self.payloads.append(payload)
        if "context" in payload:
            return json.dumps(self.answer, ensure_ascii=False)
        return super().complete(provider, system, user)

    def complete_with_timeout(self, provider, system, user, timeout):
        return self.complete(provider, system, user)


class RecordingReranker(AlwaysRelevantReranker):
    def __init__(self):
        self.questions = []

    def score(self, question, texts):
        self.questions.append(question)
        return super().score(question, texts)


class RecordingEmbedder(Embedder):
    def __init__(self):
        self.texts = []

    def encode(self, texts):
        self.texts.extend(texts)
        return super().encode(texts)


def system(chunks=None, transport=None, reranker=None):
    chunks = chunks if chunks is not None else [
        Chunk(f"c{index}", "d1", "guide.md", "糖尿病是由什么引起的？ 规律随访",
              "guide.md", "合成章节", 1, index) for index in range(2)]
    store, transport = Store(chunks), transport or DialogueTransport()
    embedder, reranker = RecordingEmbedder(), reranker or RecordingReranker()
    registry = build_registry(store, embedder, reranker, GroundedGenerator(transport),
                              Settings())
    return {"registry": registry, "store": store, "transport": transport,
            "embedder": embedder, "reranker": reranker}


def graph(version=7):
    return WorkflowRepository(WORKFLOWS).get("medical-answer", version)


def run(system, question, dialogue, version=7):
    return asyncio.run(WorkflowEngine(system["registry"]).run(
        graph(version), {"question": question, "provider": "qwen", "top_k": 5},
        dialogue=dialogue))


def test_v7_clarification_halts_before_any_retrieval():
    system_instance = system()
    result = run(system_instance, "它是什么引起的？", DialogueRun(DialogueSnapshot()))

    assert result.status == "success"
    assert result.output["answer_type"] == "clarification"
    assert result.output["citations"] == []
    assert result.trace_by_node("resolve").status == "halt"
    assert all(trace.status in {"skipped", "inactive"} for trace in result.trace
               if trace.node_id in {"dense", "bm25", "hyde", "rerank", "llm"})
    assert system_instance["store"].bm25_queries == []
    # No retrieval evidence is ever produced for a clarification.
    assert result.evidence == []


def test_v7_uses_one_semantic_question_everywhere():
    system_instance = system(transport=DialogueTransport(source="1"))
    topic = Topic(name="糖尿病", type="disease", source_turn_id="1", source_role="user")
    snapshot = DialogueSnapshot(
        state=ConversationState(revision=1, through_turn_id="1", active_topic=topic,
                                active_intent="daily_precautions",
                                active_intent_text="日常注意事项"),
        recent=(RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答"),),
        context_version=1,
    )
    dialogue = DialogueRun(snapshot)
    result = run(system_instance, FOLLOW_UP, dialogue)

    assert result.status == "success"
    assert system_instance["store"].bm25_queries == [FULL]
    assert system_instance["embedder"].texts[0] == FULL
    assert system_instance["reranker"].questions == [FULL]
    payloads = system_instance["transport"].payloads
    assert all(value["question"] == FULL for value in payloads
               if "material" in value or "evidence" in value)
    generated = next(value for value in payloads if "evidence" in value)
    assert generated["original_question"] == FOLLOW_UP
    assert all(value["question"] == FULL for value in payloads if set(value) == {"question"})
    assert dialogue.resolution.original_question == FOLLOW_UP
    assert dialogue.resolution.resolved_question == FULL


def test_v7_keeps_internal_resolution_out_of_the_public_result():
    system_instance = system(transport=DialogueTransport(source="1"))
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答"),), context_version=1)
    result = run(system_instance, FOLLOW_UP, DialogueRun(snapshot))

    assert "dialogue_resolution" not in result.output
    assert "resolution" not in result.output
    assert "state" not in result.output
    for trace in result.trace:
        metadata = json.dumps(trace.metadata, ensure_ascii=False)
        assert FOLLOW_UP not in metadata
        assert "dialogue" not in metadata
    # The raw question is generation context, never an evidence block.
    assert all(FOLLOW_UP not in item.chunk.text for item in result.evidence)


def test_v7_correction_expands_only_the_intent_and_reranks_the_fixed_question():
    chunks = [Chunk("c1", "d1", "guide.md", "规律随访与生活方式", "guide.md", "合成章节", 1, 0)]
    transport = DialogueTransport(source="1")
    system_instance = system(chunks, transport)
    snapshot = DialogueSnapshot(recent=(
        RecentTurn(turn_id="1", user="糖尿病呢？", assistant="合成回答"),), context_version=1)

    result = run(system_instance, FOLLOW_UP, DialogueRun(snapshot))

    assert result.status == "success"
    assert system_instance["store"].bm25_queries == [
        FULL, FULL + " 病因 原因 发病机制"]
    # The second round reranks with the fixed semantic question, not the expansion.
    assert system_instance["reranker"].questions == [FULL, FULL]
    corrective = result.trace_by_node("corrective")
    assert corrective.metadata["corrective"]["semantic_question"] == FULL


def test_v7_abstains_when_retrieval_is_empty():
    result = run(system(chunks=[]), "糖尿病是由什么引起的？", DialogueRun(DialogueSnapshot()))

    assert result.status == "success"
    assert result.trace_by_node("gate").status == "halt"
    assert result.output["abstained"] is True
    assert result.output["abstain_reason"] == "ABSTAIN_NO_EVIDENCE"


def test_two_dialogue_runs_do_not_share_state():
    left = system(chunks=[Chunk("l1", "d", "guide.md", "高血压如何随访？ 规律随访",
                                "guide.md", "合成章节", 1, 0)])
    right = system(chunks=[Chunk("r1", "d", "guide.md", "糖尿病是由什么引起的？ 规律随访",
                                 "guide.md", "合成章节", 1, 0)],
                   transport=DialogueTransport(topic="高血压", source="current",
                                               full="高血压如何随访？", intent="treatment",
                                               intent_text="随访", relation="topic_switch"))
    left_dialogue = DialogueRun(DialogueSnapshot())
    right_dialogue = DialogueRun(DialogueSnapshot())

    async def both():
        return await asyncio.gather(
            WorkflowEngine(left["registry"]).run(
                graph(), {"question": "糖尿病是由什么引起的？", "provider": "qwen"},
                dialogue=left_dialogue),
            WorkflowEngine(right["registry"]).run(
                graph(), {"question": "高血压如何随访？", "provider": "qwen"},
                dialogue=right_dialogue),
        )

    first, second = asyncio.run(both())

    assert first.status == "success" and second.status == "success"
    assert left_dialogue.resolution.topic.name == "糖尿病"
    assert right_dialogue.resolution.topic.name == "高血压"
    assert left["store"].bm25_queries[0] == "糖尿病是由什么引起的？"
    assert right["store"].bm25_queries[0] == "高血压如何随访？"


def test_v7_is_published_and_v6_remains_available():
    repository = WorkflowRepository(WORKFLOWS)
    v7 = repository.get("medical-answer", 7)
    v6 = repository.get("medical-answer", 6)

    assert v7.status == "published"
    # v6 stays published so a rolled-back switch can still select it explicitly.
    assert v6.status == "published"
    assert repository.get_published("medical-answer").version == 7
    assert {node.id for node in v7.nodes} == {node.id for node in v6.nodes} - {
        "pre_route", "rewrite"} | {"resolve"}
    assert {node.type for node in v6.nodes} >= {"question.pre_route", "question.rewrite"}
    edges = {(edge.source, edge.source_port, edge.target, edge.target_port)
             for edge in v7.edges}
    assert ("resolve", "query", "dense", "question") in edges
    assert ("resolve", "query", "llm", "question") in edges
    assert ("query", "question", "llm", "question") not in edges


def test_v7_resolution_is_not_reused_across_two_turns_of_one_slot():
    system_instance = system()
    dialogue = DialogueRun(DialogueSnapshot())
    run(system_instance, "糖尿病是由什么引起的？", dialogue)
    resolved_once = dialogue.resolution

    again = run(system_instance, "那它有什么症状？", dialogue)

    assert again.status == "failed"
    assert dialogue.resolution is resolved_once
