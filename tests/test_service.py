import json
import sys
from pathlib import Path

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
        block = json.loads(user)["evidence"][0]
        return json.dumps({"claims": [{"text": "应规律随访",
            "citations": [{"chunk_id": block["chunk_id"], "excerpt": "规律随访"}]}]})


def test_document_to_answer_flow_and_duplicate_upload():
    store = MemoryStore()
    transport = Transport()
    service = RagService(Settings(), store, Embedder(), Reranker(), GroundedGenerator(transport))
    client = TestClient(create_app(service=service))
    payload = {"file": ("guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())}
    first = client.post("/documents", files=payload).json()["data"]
    assert first["strategy"] == "markdown"
    assert first["duplicate"] is False
    assert len(client.get("/documents").json()["data"]) == 1
    assert client.post("/search", json={"question": "高血压"}).json()["data"][0]["chunk"]["filename"] == "guide.md"
    answer = client.post("/answers", json={"question": "高血压", "provider": "qwen"}).json()["data"]
    assert answer["abstained"] is False
    assert answer["citations"][0]["filename"] == "guide.md"
    assert client.post("/documents", files=payload).json()["data"]["duplicate"] is True
    assert len(store.chunks["guide.md"]) == 1
    assert transport.calls == 1


def test_unrelated_question_abstains_without_external_call():
    transport = Transport()
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(), GroundedGenerator(transport))
    client = TestClient(create_app(service=service))
    client.post("/documents", files={"file": ("guide.txt", "高血压患者应规律随访。".encode())})
    result = client.post("/answers", json={"question": "糖尿病"}).json()["data"]
    assert result["abstained"] is True
    assert transport.calls == 0


def test_empty_pdf_returns_structured_validation_error():
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(), GroundedGenerator(Transport()))
    response = TestClient(create_app(service=service)).post("/documents", files={"file": ("scan.pdf", b"invalid")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_DOCUMENT"
