import json
import sys
from pathlib import Path

from document_helpers import upload_and_process
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.service import RagService
from medical_rag.types import Evidence


class MemoryStore:
    def __init__(self):
        self.docs = {}
        self.chunks = {}

    def replace(self, doc, chunks, vectors):
        if doc.source_id in self.docs and self.docs[doc.source_id].sha256 == doc.sha256:
            return True
        self.docs[doc.source_id] = doc
        self.chunks[doc.source_id] = chunks
        return False

    def list_documents(self):
        return list(self.docs.values())

    def get_chunk(self, chunk_id):
        for chunks in self.chunks.values():
            for chunk in chunks:
                if chunk.id == chunk_id:
                    return chunk
        return None

    def search_dense(self, vector, limit):
        return [Evidence(chunk, 0.8) for chunks in self.chunks.values() for chunk in chunks][:limit]

    def search_bm25(self, question, limit):
        return [Evidence(chunk, 1.0) for chunks in self.chunks.values()
                for chunk in chunks if question in chunk.text][:limit]

    def ping(self):
        return True


class Embedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


class Reranker:
    def score(self, question, texts):
        return [0.9 if question in text else 0.1 for text in texts]


class Transport:
    def __init__(self):
        self.calls = 0

    def complete(self, provider, system, user):
        self.calls += 1
        payload = json.loads(user)
        if "evidence" not in payload:
            # 回答规划节点的调用只看结构与主题，不产出结论。
            return json.dumps({"sections": ["基本概念解释", "常见处理方式"]}, ensure_ascii=False)
        block = payload["evidence"][0]
        return json.dumps({"claims": [{"text": "应规律随访",
            "citations": [{"chunk_id": block["chunk_id"], "excerpt": "规律随访"}]}]})


def test_document_to_answer_flow_and_duplicate_upload():
    store = MemoryStore()
    transport = Transport()
    service = RagService(Settings(), store, Embedder(), Reranker(), GroundedGenerator(transport))
    client = TestClient(create_app(service=service))
    payload = {"file": ("guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())}
    first = upload_and_process(client, files=payload)["result"]
    assert first["strategy"] == "markdown"
    assert first["duplicate"] is False
    assert len(client.get("/documents").json()["data"]) == 1
    assert client.post("/search", json={"question": "高血压"}).json()["data"][0]["chunk"]["filename"] == "guide.md"
    answer = client.post("/answers", json={"question": "高血压", "provider": "qwen"}).json()["data"]
    assert answer["abstained"] is False
    assert answer["citations"][0]["filename"] == "guide.md"
    assert upload_and_process(client, files=payload)["result"]["duplicate"] is True
    assert len(store.chunks["guide.md"]) == 1
    # HyDE、纠错重写（单块证据低于反馈阈值后回退原问题）、规划与生成各调用一次。
    assert transport.calls == 4


def test_unrelated_question_abstains_after_optional_hyde():
    transport = Transport()
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(), GroundedGenerator(transport))
    client = TestClient(create_app(service=service))
    upload_and_process(client, files={"file": ("guide.txt", "高血压患者应规律随访。".encode())})
    result = client.post("/answers", json={"question": "糖尿病"}).json()["data"]
    assert result["abstained"] is True
    # HyDE 一次；反馈规则再花一次重写，重写回退成原问题即停，答案模型未被调用。
    assert transport.calls == 2


def test_chunk_endpoint_serves_stored_block_for_citation_display():
    transport = Transport()
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(), GroundedGenerator(transport))
    client = TestClient(create_app(service=service))
    upload_and_process(client, files={"file": ("guide.txt", "高血压患者应规律随访。".encode())})
    hit = client.post("/search", json={"question": "高血压"}).json()["data"][0]["chunk"]
    detail = client.get(f"/chunks/{hit['id']}").json()["data"]
    assert detail["chunk_id"] == hit["id"]
    assert detail["text"] == hit["text"]
    assert detail["filename"] == "guide.txt"
    assert "dense" not in detail and "sparse" not in detail
    missing = client.get("/chunks/webdoesnotexist1")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "CHUNK_NOT_FOUND"


def test_health_exposes_reranker_degradation_without_raw_errors():
    class UnavailableReranker(Reranker):
        def status(self):
            return {"available": False, "loaded": False,
                    "reason": "reranker_unavailable", "retry_in_seconds": 42.0,
                    "detail": "C:/models/secret failure"}

    service = RagService(Settings(), MemoryStore(), Embedder(), UnavailableReranker(),
                         GroundedGenerator(Transport()))
    health = TestClient(create_app(service=service)).get("/health").json()["data"]

    assert health["milvus"] is True
    assert health["reranker_available"] is False
    assert health["reranker_reason"] == "reranker_unavailable"
    assert health["reranker_retry_in_seconds"] == 42.0
    assert "detail" not in health


def test_empty_pdf_returns_structured_validation_error():
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(), GroundedGenerator(Transport()))
    client = TestClient(create_app(service=service))
    job = upload_and_process(client, files={"file": ("scan.pdf", b"invalid")})
    assert job["status"] == "failed"
    assert job["error"]["code"] == "INVALID_DOCUMENT"


def test_embedding_failure_is_dependency_error_not_invalid_document():
    class BrokenEmbedder:
        def encode(self, texts):
            raise ValueError("向量输出必须为 512 维")

    service = RagService(Settings(), MemoryStore(), BrokenEmbedder(), Reranker(), GroundedGenerator(Transport()))
    client = TestClient(create_app(service=service), raise_server_exceptions=False)
    job = upload_and_process(client, files={"file": ("guide.txt", "高血压管理。".encode())})
    assert job["status"] == "retrying"
    assert job["error"]["code"] in {"NODE_EXECUTION_ERROR", "DEPENDENCY_ERROR"}


def test_whitespace_question_is_validation_error():
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(), GroundedGenerator(Transport()))
    response = TestClient(create_app(service=service)).post("/search", json={"question": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_QUERY"


def test_source_identifier_is_limited_by_utf8_bytes():
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(), GroundedGenerator(Transport()))
    response = TestClient(create_app(service=service)).post(
        "/documents", data={"source_id": "医" * 200},
        files={"file": ("guide.txt", "高血压管理。".encode())},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_SOURCE"


class FencedTransport(Transport):
    """模型把 JSON 包在 Markdown 围栏里：OpenAI 兼容端点最常见的非标准输出。"""

    def complete(self, provider, system, user):
        payload = json.loads(user)
        if "evidence" not in payload:
            return json.dumps({"sections": ["基本概念解释"]}, ensure_ascii=False)
        block = payload["evidence"][0]
        return ("```json\n" + json.dumps({"claims": [{"text": "应规律随访", "citations": [
            {"chunk_id": block["chunk_id"]}]}]}, ensure_ascii=False) + "\n```")


def test_fenced_model_output_is_answered_through_the_api():
    """围栏只是格式问题，`/answers` 不能把它变成“现有资料不足以回答该问题”。"""
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(FencedTransport()))
    client = TestClient(create_app(service=service))
    upload_and_process(client, files={"file": ("guide.md", "高血压患者应规律随访。".encode())})

    answer = client.post("/answers", json={"question": "高血压", "provider": "qwen"}).json()["data"]

    assert answer["abstained"] is False
    assert answer["answer"].startswith("应规律随访 [")
    assert answer["output_repaired"] == "fence"


def test_unparsable_model_output_is_reported_as_a_generation_fault_through_the_api():
    class GarbageTransport(Transport):
        def complete(self, provider, system, user):
            payload = json.loads(user)
            if "evidence" not in payload:
                return json.dumps({"sections": ["基本概念解释"]}, ensure_ascii=False)
            return "抱歉，现有资料不足以回答该问题。"

    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(GarbageTransport()))
    client = TestClient(create_app(service=service))
    upload_and_process(client, files={"file": ("guide.md", "高血压患者应规律随访。".encode())})

    answer = client.post("/answers", json={"question": "高血压", "provider": "qwen"}).json()["data"]

    assert answer["abstained"] is True
    assert answer["abstain_reason"] == "ABSTAIN_PARSE_ERROR"
    # 正文必须是“生成异常”，而不是把模型输出故障写成资料不足。
    assert "生成异常" in answer["answer"]

# -- dialogue feature flag and answer-version selection ----------------------

import pytest

from medical_rag.dialogue_flags import dialogue_enabled
from medical_rag.service import selected_answer_version
from medical_rag.workflow.engine import WorkflowRunResult
from medical_rag.workflow.repository import WorkflowRepository

DEFAULT_WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


@pytest.mark.parametrize("enabled,requested,expected", [
    (False, None, 6), (False, 6, 6), (True, None, 7), (True, 6, 6), (True, 7, 7),
])
def test_answer_version_selection(enabled, requested, expected):
    assert selected_answer_version(enabled, requested) == expected


def test_disabled_flag_rejects_explicit_v7():
    from medical_rag.types import ApiError

    with pytest.raises(ApiError) as caught:
        selected_answer_version(False, 7)
    assert caught.value.code == "DIALOGUE_FEATURE_DISABLED"


@pytest.mark.parametrize("value,expected", [
    (None, False), ("true", True), ("1", True), ("TRUE", True),
    ("false", False), ("0", False), (" False ", False),
])
def test_dialogue_flag_reads_only_explicit_spellings(value, expected):
    environ = {} if value is None else {"DIALOGUE_STATE_ENABLED": value}
    assert dialogue_enabled(environ) is expected


def test_dialogue_flag_rejects_unknown_spelling_without_echoing_it():
    with pytest.raises(ValueError) as caught:
        dialogue_enabled({"DIALOGUE_STATE_ENABLED": "yes-please"})
    assert "yes-please" not in str(caught.value)


class RecordingRepository:
    def __init__(self, definition):
        self.definition = definition
        self.calls = []

    def get_published(self, workflow_id):
        self.calls.append(("published", workflow_id))
        return self.definition

    def get(self, workflow_id, version=None):
        self.calls.append(("get", workflow_id, version))
        return self.definition


class StubEngine:
    async def run(self, definition, inputs, **kwargs):
        return WorkflowRunResult("run-1", "success",
                                 {"answer": "合成回答", "citations": [], "abstained": False}, [])


def _version_service(enabled):
    import asyncio

    definition = WorkflowRepository(DEFAULT_WORKFLOWS).get("medical-answer", 6)
    repository = RecordingRepository(definition)
    service = RagService(Settings(dialogue_state_enabled=enabled), MemoryStore(), Embedder(),
                         Reranker(), GroundedGenerator(Transport()))
    service.repository = repository
    service.engine = StubEngine()
    asyncio.run(service.run_workflow("medical-answer", {"question": "高血压", "provider": "qwen"}))
    return repository.calls


def test_disabled_switch_selects_explicit_v6_not_the_latest_published():
    assert _version_service(False) == [("get", "medical-answer", 6)]


def test_enabled_switch_selects_v7():
    assert _version_service(True) == [("get", "medical-answer", 7)]


def test_other_workflows_keep_the_repository_version_semantics():
    import asyncio

    definition = WorkflowRepository(DEFAULT_WORKFLOWS).get("medical-search", 1)
    repository = RecordingRepository(definition)
    service = RagService(Settings(dialogue_state_enabled=True), MemoryStore(), Embedder(),
                         Reranker(), GroundedGenerator(Transport()))
    service.repository = repository
    service.engine = StubEngine()
    asyncio.run(service.run_workflow("medical-search", {"question": "高血压"}))
    assert repository.calls == [("published", "medical-search")]


# -- ephemeral run slot ------------------------------------------------------

def test_ephemeral_context_run_is_not_cached_or_readable(tmp_path):
    import asyncio

    from medical_rag.dialogue_models import DialogueRun, DialogueSnapshot
    from medical_rag.types import ApiError

    definition = WorkflowRepository(DEFAULT_WORKFLOWS).get("medical-answer", 7)
    WorkflowRepository(tmp_path).save(definition.model_copy(update={"status": "published"}))
    service = RagService(Settings(dialogue_state_enabled=True), MemoryStore(), Embedder(),
                         Reranker(), GroundedGenerator(Transport()), workflow_root=tmp_path)
    service.engine = StubEngine()

    result = asyncio.run(service.run_workflow(
        "medical-answer", {"question": "糖尿病是由什么引起的？", "provider": "qwen"},
        dialogue=DialogueRun(DialogueSnapshot()), retain_run=False))

    assert result.status == "success"
    assert service.runs == {}
    with pytest.raises(ApiError):
        service.get_run(result.run_id)

    cached = asyncio.run(service.run_workflow(
        "medical-answer", {"question": "糖尿病是由什么引起的？", "provider": "qwen"},
        dialogue=DialogueRun(DialogueSnapshot()), retain_run=True))
    assert service.get_run(cached.run_id) is cached

def test_disabled_switch_stays_on_v6_even_when_v7_is_published():
    import asyncio

    v7 = WorkflowRepository(DEFAULT_WORKFLOWS).get("medical-answer", 7).model_copy(
        update={"status": "published"})
    repository = RecordingRepository(v7)
    service = RagService(Settings(dialogue_state_enabled=False), MemoryStore(), Embedder(),
                         Reranker(), GroundedGenerator(Transport()))
    service.repository = repository
    service.engine = StubEngine()

    asyncio.run(service.run_workflow("medical-answer", {"question": "高血压", "provider": "qwen"}))

    assert repository.calls == [("get", "medical-answer", 6)]
