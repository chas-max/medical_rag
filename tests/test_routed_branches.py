"""End-to-end tests for the 5-branch routed medical RAG workflow and route nodes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from medical_rag.config import Settings
from medical_rag.generation import (
    ABSTAIN_INSUFFICIENT_COVERAGE,
    ABSTAIN_NO_EVIDENCE,
    NUMERIC_EXACT_DIRECTIVE,
    GroundedGenerator,
    _is_meta_refusal_claim,
)
from medical_rag.types import SCORE_DENSE, SCORE_RERANK, Chunk, Evidence
from medical_rag.workflow.bootstrap import build_registry
from medical_rag.workflow.contracts import ExecutionContext
from medical_rag.workflow.definition import WorkflowDefinition, WorkflowEdge, WorkflowNode
from medical_rag.workflow.engine import WorkflowEngine
from medical_rag.workflow.nodes.routing_branches import listwise_rerank
from medical_rag.workflow.repository import WorkflowRepository
from medical_rag.workflow.validator import validate_workflow

WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / "workflows"


class FakeStore:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.dense_queries = 0
        self.bm25_queries: list[str] = []

    def search_dense(self, vector: list[float], limit: int) -> list[Evidence]:
        self.dense_queries += 1
        return [Evidence(chunk, 0.85, score_kind=SCORE_DENSE) for chunk in self.chunks][:limit]

    def search_bm25(self, question: str, limit: int) -> list[Evidence]:
        self.bm25_queries.append(question)
        hits = [
            Evidence(chunk, 0.9, score_kind=SCORE_DENSE)
            for chunk in self.chunks
            if any(tok in (chunk.filename + chunk.text) for tok in question.split())
        ]
        return (hits or [Evidence(chunk, 0.5, score_kind=SCORE_DENSE) for chunk in self.chunks])[:limit]


class FakeEmbedder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 15 for _ in texts]


class ScoreMapReranker:
    def __init__(self, scores_by_id: dict[str, float]):
        self.scores_by_id = scores_by_id
        self.calls: list[str] = []

    def score_with_kind(self, question: str, texts: list[str]) -> tuple[list[float], str]:
        self.calls.append(question)
        scores = []
        for text in texts:
            matched = next(
                (val for cid, val in self.scores_by_id.items() if cid in text),
                0.75,
            )
            scores.append(matched)
        return scores, SCORE_RERANK


class RoutedFakeTransport:
    def __init__(
        self,
        *,
        route: str = "direct_lookup",
        confidence: float = 0.92,
        decompose_questions: list[str] | None = None,
        sufficiency_payload: object = None,
        sufficiency_error: Exception | None = None,
        listwise_ranked: list[int] | None = None,
        listwise_error: Exception | None = None,
        draft_payload: dict | None = None,
        repair_payload: dict | None = None,
    ):
        self.route = route
        self.confidence = confidence
        self.decompose_questions = decompose_questions or ["子问题1", "子问题2"]
        self.sufficiency_payload = sufficiency_payload if sufficiency_payload is not None else {
            "sufficient": True,
            "missing_conditions": [],
        }
        self.sufficiency_error = sufficiency_error
        self.listwise_ranked = listwise_ranked
        self.listwise_error = listwise_error
        self.draft_payload = draft_payload or {
            "claims": [{"text": "推荐规范随访 [placeholder]", "citations": [{"chunk_id": "c1"}]}],
            "gaps": [],
        }
        self.repair_payload = repair_payload or {"repaired": []}
        self.plan_calls = 0
        self.generation_systems: list[str] = []
        self.classify_calls: list[dict] = []
        self.matrix_decompose_calls = 0

    def _dispatch(self, system: str, user: str) -> str:
        if "你只解析会话问题" in system:
            payload = json.loads(user)
            return json.dumps(
                {
                    "status": "resolved",
                    "relation": "standalone",
                    "resolved_question": payload.get("question", ""),
                },
                ensure_ascii=False,
            )
        if "意图分类器" in system:
            self.classify_calls.append(json.loads(user))
            return json.dumps({"route": self.route, "confidence": self.confidence})
        if "对比维度" in system or "matrix" in system:
            self.matrix_decompose_calls += 1
            return json.dumps({"questions": self.decompose_questions}, ensure_ascii=False)
        if "拆分为至多三个" in system:
            return json.dumps({"questions": self.decompose_questions[:3]}, ensure_ascii=False)
        if "医疗证据覆盖性安全门控" in system or "医疗证据充分性审查器" in system:
            if self.sufficiency_error is not None:
                raise self.sufficiency_error
            return json.dumps(self.sufficiency_payload, ensure_ascii=False)
        if "多文档医疗证据重排器" in system:
            if self.listwise_error is not None:
                raise self.listwise_error
            payload = json.loads(user)
            count = len(payload.get("candidates", []))
            ranked = self.listwise_ranked if self.listwise_ranked is not None else list(range(count))
            return json.dumps({"ranked_ids": ranked})
        if "回答应遵循的结构要点" in system:
            self.plan_calls += 1
            return json.dumps({"sections": ["要点一", "要点二"]}, ensure_ascii=False)
        if "医疗数值严格校对助手" in system:
            return json.dumps(self.repair_payload, ensure_ascii=False)
        if "你是一名专业医疗知识助手" in system:
            self.generation_systems.append(system)
            return json.dumps(self.draft_payload, ensure_ascii=False)
        if "保留原意，把用户问题改写" in system or "把用户当前问题改写" in system:
            return json.dumps({"question": "补充高血压与肾病联合治疗指征"}, ensure_ascii=False)
        self.generation_systems.append(system)
        return json.dumps(self.draft_payload, ensure_ascii=False)

    def complete_with_timeout(
        self,
        provider: str,
        system: str,
        user: str,
        timeout_seconds: float,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        max_attempts: int = 2,
    ) -> str:
        return self._dispatch(system, user)

    def complete(self, provider: str, system: str, user: str) -> str:
        return self._dispatch(system, user)

    def stream_chat(
        self,
        provider: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_content=None,
    ) -> dict:
        text = self._dispatch(messages[0]["content"], messages[1]["content"])
        if on_content is not None:
            for idx in range(0, len(text), 17):
                on_content(text[idx:idx + 17])
        return {"content": text, "tool_calls": [], "finish_reason": "stop"}


def _build_routed_engine(
    chunks: list[Chunk],
    scores_by_id: dict[str, float],
    transport: RoutedFakeTransport,
) -> tuple[WorkflowDefinition, WorkflowEngine]:
    store = FakeStore(chunks)
    reranker = ScoreMapReranker(scores_by_id)
    generator = GroundedGenerator(transport)
    settings = Settings(evidence_threshold=0.4, candidate_k=20, rerank_k=90, top_k=5)
    registry = build_registry(store, FakeEmbedder(), reranker, generator, settings)
    definition = WorkflowRepository(WORKFLOWS_DIR).get_published("medical-answer-routed")
    return definition, WorkflowEngine(registry)


def test_routed_workflow_validates_cleanly() -> None:
    transport = RoutedFakeTransport()
    registry = build_registry(
        FakeStore([]),
        FakeEmbedder(),
        ScoreMapReranker({}),
        GroundedGenerator(transport),
        Settings(),
    )
    definition = WorkflowRepository(WORKFLOWS_DIR).get_published("medical-answer-routed")
    issues = validate_workflow(definition, registry)
    assert issues == []


def test_evidence_select_rejects_ungated_upstream() -> None:
    transport = RoutedFakeTransport()
    registry = build_registry(
        FakeStore([]),
        FakeEmbedder(),
        ScoreMapReranker({}),
        GroundedGenerator(transport),
        Settings(),
    )
    definition = WorkflowDefinition(
        id="unsafe-select",
        name="Unsafe Select",
        version=1,
        status="draft",
        nodes=[
            WorkflowNode.model_validate({"id": "query", "type": "query.input"}),
            WorkflowNode.model_validate({"id": "dense", "type": "retrieval.dense"}),
            WorkflowNode.model_validate({"id": "rerank", "type": "retrieval.rerank"}),
            WorkflowNode.model_validate({"id": "select", "type": "evidence.select"}),
            WorkflowNode.model_validate({"id": "llm", "type": "generation.llm"}),
            WorkflowNode.model_validate({"id": "citation", "type": "citation.validate"}),
            WorkflowNode.model_validate({"id": "output", "type": "answer.output"}),
        ],
        edges=[
            WorkflowEdge.model_validate({"id": "e1", "source": "query", "source_port": "question", "target": "dense", "target_port": "question"}),
            WorkflowEdge.model_validate({"id": "e2", "source": "query", "source_port": "question", "target": "rerank", "target_port": "question"}),
            WorkflowEdge.model_validate({"id": "e3", "source": "dense", "source_port": "evidence", "target": "rerank", "target_port": "evidence"}),
            WorkflowEdge.model_validate({"id": "e4", "source": "rerank", "source_port": "evidence", "target": "select", "target_port": "sources"}),
            WorkflowEdge.model_validate({"id": "e5", "source": "query", "source_port": "question", "target": "llm", "target_port": "question"}),
            WorkflowEdge.model_validate({"id": "e6", "source": "select", "source_port": "evidence", "target": "llm", "target_port": "evidence"}),
            WorkflowEdge.model_validate({"id": "e7", "source": "llm", "source_port": "draft", "target": "citation", "target_port": "draft"}),
            WorkflowEdge.model_validate({"id": "e8", "source": "llm", "source_port": "evidence", "target": "citation", "target_port": "evidence"}),
            WorkflowEdge.model_validate({"id": "e9", "source": "citation", "source_port": "answer", "target": "output", "target_port": "answer"}),
        ],
    )
    issues = validate_workflow(definition, registry)
    assert any(issue.code == "UNSAFE_ANSWER_FLOW" and issue.node_id == "select" for issue in issues)


def test_routed_direct_lookup_skips_heavy_nodes_and_planning() -> None:
    chunks = [Chunk("c1", "d1", "高血压指南.pdf", "c1 成人高血压诊断标准为140/90mmHg。", "高血压指南.pdf", "诊断", 1, 0, parent_id="p1")]
    transport = RoutedFakeTransport(
        route="direct_lookup",
        confidence=0.95,
        draft_payload={
            "claims": [{"text": "成人高血压诊断标准为140/90mmHg。", "citations": [{"chunk_id": "c1"}]}],
            "gaps": [],
        },
    )
    definition, engine = _build_routed_engine(chunks, {"c1": 0.92}, transport)
    run = asyncio.run(engine.run(definition, {"question": "成人高血压诊断标准是什么？"}))
    trace = {entry.node_id: entry for entry in run.trace}

    assert run.status == "success"
    assert run.output["abstained"] is False
    assert trace["rerank_direct"].status == "success"
    assert trace["gate_direct"].status == "success"
    assert trace["decompose_cmp"].status == "inactive"
    assert trace["rerank_cmp"].status == "inactive"
    assert trace["corrective_cmp"].status == "skipped"
    assert trace["listwise_cmp"].status == "skipped"
    assert trace["order_proc"].status == "skipped"
    assert trace["suff_scen"].status == "skipped"
    assert trace["plan"].status == "success"
    assert trace["plan"].metadata["skipped_by_route"] == "direct_lookup"
    assert transport.plan_calls == 0


def test_routed_numeric_exact_directive_repair_sse_and_meta_refusal() -> None:
    assert _is_meta_refusal_claim("提供的糖尿病防治指南及食养指南中均未包含具体剂量")
    gen = GroundedGenerator(None)
    refused = gen.validate(
        json.dumps(
            {
                "claims": [
                    {
                        "text": "提供的糖尿病防治指南及食养指南中均未包含具体的用药调整方案。",
                        "citations": [{"chunk_id": "c1"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        [Evidence(Chunk("c1", "d1", "g.pdf", "相关背景", "g.pdf", "总则", 1, 0), 0.9, score_kind=SCORE_RERANK)],
    )
    assert refused["abstained"] is True
    assert refused["abstain_reason"] == ABSTAIN_NO_EVIDENCE

    chunks = [
        Chunk("c1", "d1", "用药指南.pdf", "c1 起始剂量每日30mg。", "用药指南.pdf", "剂量", 1, 0, parent_id="p1"),
        Chunk("c2", "d1", "用药指南.pdf", "c2 单次剂量上限为500mg。", "用药指南.pdf", "剂量", 2, 0, parent_id="p2"),
    ]
    transport = RoutedFakeTransport(
        route="numeric_exact",
        confidence=0.93,
        draft_payload={
            "claims": [
                {"text": "起始剂量每日30mg。", "citations": [{"chunk_id": "c1"}]},
                {"text": "单次剂量上限为5000mg。", "citations": [{"chunk_id": "c2"}]},
            ],
            "gaps": [],
        },
        repair_payload={"repaired": [{"id": 0, "text": "单次剂量上限为500mg。"}]},
    )
    definition, engine = _build_routed_engine(chunks, {"c1": 0.92, "c2": 0.90}, transport)
    events: list[dict] = []
    run = asyncio.run(
        engine.run(
            definition,
            {"question": "二甲双胍的起始剂量和单次剂量上限分别是多少？"},
            emit=events.append,
        )
    )
    trace = {entry.node_id: entry for entry in run.trace}

    assert run.status == "success"
    assert run.output["abstained"] is False
    assert "30mg" in run.output["answer"]
    assert "500mg" in run.output["answer"]
    assert trace["citation"].metadata["numeric_repaired"] == 1
    assert any(NUMERIC_EXACT_DIRECTIVE.strip() in sys for sys in transport.generation_systems)
    claim_events = [evt for evt in events if evt.get("event") == "claim"]
    assert any("500mg" in evt["data"]["text"] for evt in claim_events)


def test_routed_procedure_ordered_restores_page_order_keeping_scores() -> None:
    c_late = Chunk("c_late", "doc_main", "操作规范.pdf", "c_late 第5页：第三步复查评估", "操作规范.pdf", "步骤3", 5, 1, parent_id="p_late")
    c_early = Chunk("c_early", "doc_main", "操作规范.pdf", "c_early 第2页：第一步术前核查", "操作规范.pdf", "步骤1", 2, 0, parent_id="p_early")
    c_other = Chunk("c_other", "doc_sec", "辅助手册.pdf", "c_other 第1页：补充参考条款", "辅助手册.pdf", "补充", 1, 0, parent_id="p_other")
    transport = RoutedFakeTransport(
        route="procedure_ordered",
        confidence=0.9,
        draft_payload={
            "claims": [{"text": "按先核查后复查顺序执行 [placeholder]", "citations": [{"chunk_id": "c_early"}]}],
            "gaps": [],
        },
    )
    definition, engine = _build_routed_engine(
        [c_late, c_other, c_early],
        {"c_late": 0.95, "c_other": 0.85, "c_early": 0.72},
        transport,
    )
    run = asyncio.run(engine.run(definition, {"question": "该操作规范的完整执行流程与步骤顺序是什么？"}))
    trace = {entry.node_id: entry for entry in run.trace}

    assert run.status == "success"
    assert trace["order_proc"].status == "success"
    assert trace["order_proc"].metadata["reordered"] is True
    assert [ev.chunk.id for ev in run.evidence] == ["c_early", "c_late", "c_other"]
    assert [round(ev.score, 2) for ev in run.evidence] == [0.72, 0.95, 0.85]


def test_routed_scenario_guarded_abstains_or_fails_open_on_timeout() -> None:
    chunks = [Chunk("c1", "d1", "糖尿病指南.pdf", "c1 成人常规血糖控制一般原则。", "糖尿病指南.pdf", "总则", 1, 0, parent_id="p1")]
    transport_halt = RoutedFakeTransport(
        route="scenario_guarded",
        confidence=0.91,
        sufficiency_payload={"sufficient": False, "missing_conditions": ["妊娠合并重度肾衰禁用标准"]},
    )
    definition, engine = _build_routed_engine(chunks, {"c1": 0.88}, transport_halt)
    run_halt = asyncio.run(
        engine.run(definition, {"question": "一位妊娠32周合并重度肾衰的患者如何精准调整某药剂量？"})
    )
    trace_halt = {entry.node_id: entry for entry in run_halt.trace}
    assert run_halt.status == "success"
    assert run_halt.output["abstained"] is True
    assert run_halt.output["abstain_reason"] == ABSTAIN_INSUFFICIENT_COVERAGE
    assert trace_halt["suff_scen"].status == "halt"
    assert trace_halt["llm"].metadata["reason"] == "halted_upstream"

    transport_timeout = RoutedFakeTransport(
        route="scenario_guarded",
        confidence=0.91,
        sufficiency_error=TimeoutError("sufficiency timed out"),
        draft_payload={
            "claims": [{"text": "应遵循个体化血糖管理一般原则。", "citations": [{"chunk_id": "c1"}]}],
            "gaps": [],
        },
    )
    definition2, engine2 = _build_routed_engine(chunks, {"c1": 0.88}, transport_timeout)
    run_open = asyncio.run(
        engine2.run(definition2, {"question": "一位妊娠32周合并重度肾衰的患者如何精准调整某药剂量？"})
    )
    trace_open = {entry.node_id: entry for entry in run_open.trace}
    assert run_open.status == "success"
    assert run_open.output["abstained"] is False
    assert trace_open["suff_scen"].metadata["degraded"] is True
    assert trace_open["suff_scen"].metadata["degraded_reason"] == "sufficiency_fallback"


def test_routed_multi_doc_compare_matrix_corrective_and_listwise() -> None:
    c_htn1 = Chunk("c_htn1", "d1", "中国高血压防治指南.pdf", "c_htn1 高血压靶目标小于140/90mmHg", "中国高血压防治指南.pdf", "目标", 1, 0, parent_id="p_htn1")
    c_htn2 = Chunk("c_htn2", "d1", "中国高血压防治指南.pdf", "c_htn2 不同危险分层随访管理", "中国高血压防治指南.pdf", "随访", 2, 0, parent_id="p_htn2")
    c_ckd = Chunk("c_ckd", "d2", "慢性肾脏病管理指南.pdf", "c_ckd CKD推荐使用ACEI或ARB", "慢性肾脏病管理指南.pdf", "用药", 1, 0, parent_id="p_ckd")
    transport = RoutedFakeTransport(
        route="multi_doc_compare",
        confidence=0.94,
        decompose_questions=[
            "高血压患者靶目标与一线用药",
            "不同危险分层血压控制差异",
            "高血压与CKD随访频率差异",
            "特殊合并症禁忌证对比",
        ],
        listwise_ranked=[2, 0, 1],
        draft_payload={
            "claims": [
                {"text": "高血压控制目标一般小于140/90mmHg。", "citations": [{"chunk_id": "c_htn1"}]},
                {"text": "慢性肾脏病推荐优先使用ACEI或ARB。", "citations": [{"chunk_id": "c_ckd"}]},
            ],
            "gaps": [],
        },
    )

    class CompareStore(FakeStore):
        def __init__(self, chunks: list[Chunk]):
            super().__init__(chunks)
            self.ckd_queries = 0

        def search_dense(self, vector: list[float], limit: int) -> list[Evidence]:
            return [
                Evidence(c_htn1, 0.85, score_kind=SCORE_DENSE),
                Evidence(c_htn2, 0.82, score_kind=SCORE_DENSE),
            ]

        def search_bm25(self, question: str, limit: int) -> list[Evidence]:
            self.bm25_queries.append(question)
            if "慢性肾脏病" in question:
                self.ckd_queries += 1
                # 首轮 multi 会请求主问和拆分句（共2次含该词），第3次起（corrective gap 补检）命中 c_ckd
                if self.ckd_queries >= 3:
                    return [Evidence(c_ckd, 0.92, score_kind=SCORE_DENSE)]
            return [
                Evidence(c_htn1, 0.90, score_kind=SCORE_DENSE),
                Evidence(c_htn2, 0.86, score_kind=SCORE_DENSE),
            ]

    store = CompareStore([c_htn1, c_htn2, c_ckd])
    reranker = ScoreMapReranker({"c_htn1": 0.91, "c_htn2": 0.87, "c_ckd": 0.83})
    registry = build_registry(
        store,
        FakeEmbedder(),
        reranker,
        GroundedGenerator(transport),
        Settings(evidence_threshold=0.4, candidate_k=20, rerank_k=90, top_k=5),
    )
    definition = WorkflowRepository(WORKFLOWS_DIR).get_published("medical-answer-routed")
    run = asyncio.run(
        WorkflowEngine(registry).run(
            definition,
            {"question": "对比《中国高血压防治指南》与《慢性肾脏病管理指南》的推荐差异"},
        )
    )
    trace = {entry.node_id: entry for entry in run.trace}

    assert run.status == "success"
    assert run.output["abstained"] is False
    assert transport.matrix_decompose_calls == 1
    assert trace["corrective_cmp"].status == "success"
    assert trace["corrective_cmp"].metadata["corrective"]["stop_reason"] == "gap_retried"
    assert trace["corrective_cmp"].metadata["corrective"]["before"] == 2
    assert trace["corrective_cmp"].metadata["corrective"]["after"] == 3
    assert trace["listwise_cmp"].status == "success"
    assert [ev.chunk.id for ev in run.evidence] == ["c_ckd", "c_htn1", "c_htn2"]
    assert all(ev.score_kind == SCORE_RERANK for ev in run.evidence)

    # Verify listwise timeout fails open without changing original ranking
    timeout_ctx = ExecutionContext("run-lw", "medical-answer-routed", {})
    ev1 = Evidence(c_htn1, 0.91, score_kind=SCORE_RERANK)
    ev2 = Evidence(c_ckd, 0.83, score_kind=SCORE_RERANK)
    lw_transport = RoutedFakeTransport(listwise_error=TimeoutError("listwise timeout"))
    fallback = listwise_rerank(
        {"question": "对比指南", "evidence": [ev1, ev2]},
        {"top_k": 2, "timeout_seconds": 1.0},
        timeout_ctx,
        lw_transport,
        "qwen",
    )
    assert [ev.chunk.id for ev in fallback["evidence"]] == ["c_htn1", "c_ckd"]
    assert timeout_ctx.node_metadata["degraded"] is True
    assert timeout_ctx.node_metadata["degraded_reason"] == "listwise_fallback"
