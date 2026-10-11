import json

import pytest
from fastapi.testclient import TestClient

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator
from medical_rag.service import RagService
from medical_rag.types import ApiError
from test_service import Embedder, MemoryStore, Reranker


def client(tmp_path):
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(object()), workflow_root=tmp_path)
    return TestClient(create_app(service=service)), service


def test_expanded_workflow_is_listed_and_rejects_invalid_query(tmp_path):
    api, _ = client(tmp_path)
    assert any(item["id"] == "medical-answer-expanded"
               for item in api.get("/workflows").json()["data"])
    assert any(item["type"] == "question.router" for item in api.get("/nodes").json()["data"])
    assert api.post("/workflows/medical-answer-expanded/runs",
                    json={"inputs": {"question": " "}}).json()["error"]["code"] == "INVALID_QUERY"
    invalid_top_k = api.post("/workflows/medical-answer-expanded/runs",
                             json={"inputs": {"question": "高血压", "top_k": 21}})
    assert invalid_top_k.status_code == 422
    assert invalid_top_k.json()["error"]["code"] == "INVALID_QUERY"
    invalid_provider = api.post(
        "/workflows/medical-answer-expanded/runs",
        json={"inputs": {"question": "高血压", "provider": 7}},
    )
    assert invalid_provider.status_code == 422
    assert invalid_provider.json()["error"]["code"] == "INVALID_PROVIDER"


@pytest.mark.parametrize("chosen,transform_reply", [
    ("direct", None),
    ("rewrite", '{"question":"高血压随访要求？"}'),
    ("decompose", '{"questions":["高血压随访频率？","高血压随访内容？"]}'),
    ("hyde", '{"hypothetical":"高血压患者应规律随访。"}'),
    ("stepback", '{"question":"高血压管理原则？"}'),
])
def test_expanded_workflow_routes_one_branch_and_preserves_cited_answer(
        tmp_path, chosen, transform_reply):
    class RoutedTransport:
        def __init__(self):
            self.calls = []

        def complete(self, provider, system, user):
            assert provider == "qwen"
            payload = json.loads(user)
            self.calls.append(payload)
            if "分类为" in system:
                return json.dumps({"route": chosen})
            if "evidence" not in payload:
                return transform_reply
            chunk = payload["evidence"][0]
            return json.dumps({"claims": [{"text": "应规律随访", "citations": [
                {"chunk_id": chunk["chunk_id"], "excerpt": "规律随访"}]}]})

    transport = RoutedTransport()
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(transport), workflow_root=tmp_path)
    api = TestClient(create_app(service=service))
    api.post("/documents", files={"file": (
        "guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())})
    response = api.post("/workflows/medical-answer-expanded/runs",
                        json={"inputs": {"question": "高血压", "top_k": 5}})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "success"
    assert data["output"]["abstained"] is False
    assert data["output"]["citations"][0]["excerpt"] == "规律随访"
    assert len(transport.calls) == (2 if chosen == "direct" else 3)
    for name in ("rewrite", "decompose", "hyde", "stepback"):
        trace = next(item for item in data["trace"] if item["node_id"] == name)
        assert trace["status"] == ("success" if name == chosen else "inactive")
    old = api.post("/answers", json={"question": "高血压"})
    assert old.status_code == 200
    assert service.last_workflow_id == "medical-answer"


def test_expanded_workflow_model_error_fails_instead_of_abstaining(tmp_path):
    class BrokenTransport:
        def complete(self, provider, system, user):
            raise ApiError("MODEL_RATE_LIMITED", "生成模型请求过于频繁，请稍后重试", 429)

    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(BrokenTransport()), workflow_root=tmp_path)
    response = TestClient(create_app(service=service)).post(
        "/workflows/medical-answer-expanded/runs",
        json={"inputs": {"question": "高血压", "top_k": 5}})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert next(trace for trace in data["trace"] if trace["node_id"] == "router")["error"]["code"] == "MODEL_RATE_LIMITED"
    assert data["output"] == {}
