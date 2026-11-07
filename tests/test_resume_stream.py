"""Integration coverage for the fixed chat API and HyDE retrieval path."""

import json

from fastapi.testclient import TestClient
from test_service import Embedder, MemoryStore, Reranker
from test_stream_api import StreamTransport, plan_reply, read_events

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.profile_models import IntentDecision
from medical_rag.service import RagService
from medical_rag.types import Chunk


def test_fixed_chat_stream_generates_verified_claims_from_real_evidence():
    class Analyzer:
        def analyze(self, question, snapshot):
            return IntentDecision(needs_answer=True)

    class Model(StreamTransport):
        def complete(self, provider, system, user):
            self.completes += 1
            if "模拟答案" in system:
                return json.dumps({"hypothetical": "假设内容"}, ensure_ascii=False)
            return plan_reply()

    store = MemoryStore()
    store.chunks["guide"] = [Chunk("chunk-1", "doc", "guide", "高血压患者应规律随访。",
                                   "guide.md", "", 1, 0)]
    model = Model()
    service = RagService(Settings(), store, Embedder(), Reranker(), GroundedGenerator(model))
    client = TestClient(create_app(service=service, profile_analyzer=Analyzer()))
    response = client.post("/chat/runs/stream", json={"request_id": "r1", "conversation_id": "c1",
                                                     "inputs": {"question": "高血压"}})
    assert response.status_code == 200
    events = read_events(response.text)
    assert [name for name, _ in events].count("claim") == 2
    result = events[-1][1]
    assert result["status"] == "success"
    assert result["output"]["citations"][0]["chunk_id"] == "chunk-1"
    assert "假设内容" not in result["output"]["answer"]
    # HyDE、纠错重写（单块证据低于反馈阈值后回退原问题）、规划各一次 complete。
    assert model.completes == 3 and model.streams == 1
    assert client.get("/conversations/c1").json()["data"]["messages"][-1]["content"] == result["output"]["answer"]
