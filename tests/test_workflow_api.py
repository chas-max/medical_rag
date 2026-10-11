import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator, OpenAITransport
from medical_rag.service import RagService
from medical_rag.types import ApiError

from test_service import Embedder, MemoryStore, Reranker, Transport


def client(tmp_path: Path):
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(Transport()), workflow_root=tmp_path)
    return TestClient(create_app(service=service)), service


def test_workflow_api_lists_nodes_and_published_workflows(tmp_path):
    api, service = client(tmp_path)
    nodes = api.get("/nodes")
    workflows = api.get("/workflows")
    assert nodes.json()["success"] is True
    assert any(node["type"] == "query.input" for node in nodes.json()["data"])
    assert any(item["id"] == "medical-answer" and item["status"] == "published"
               for item in workflows.json()["data"])


def test_node_catalog_marks_input_and_output_nodes_fixed(tmp_path):
    api, _ = client(tmp_path)
    catalog = api.get("/nodes").json()["data"]

    assert {item["type"] for item in catalog if item["fixed"]} == {
        "query.input", "document.input", "answer.output", "search.output",
        "document.output",
    }
    assert {item["type"] for item in catalog if item["canonical"]} == {
        "query.input", "answer.output",
    }


def test_run_schema_is_derived_from_the_graph(tmp_path):
    """运行入参不再是按工作流 ID 写死的字段表：图上没连线的入参就不是必填。"""
    api, _ = client(tmp_path)
    wired = api.get("/workflows/medical-answer/run-schema").json()["data"]
    direct = api.get("/workflows/medical-answer-direct/run-schema").json()["data"]

    assert [(item["name"], item["dtype"], item["required"]) for item in wired] == [
        ("provider", "string", True), ("question", "string", True), ("top_k", "integer", True)]
    assert [(item["name"], item["required"]) for item in direct] == [
        ("provider", False), ("question", True), ("top_k", False)]
    assert {item["node_id"] for item in direct} == {"query"}
    assert api.get("/workflows/does-not-exist/run-schema").json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


def test_workflow_run_and_lookup_use_envelope(tmp_path):
    api, service = client(tmp_path)
    response = api.post("/workflows/medical-answer/runs", json={"inputs": {
        "question": "高血压", "provider": "qwen", "top_k": 5}})
    assert response.status_code == 200
    run = response.json()["data"]
    assert run["output"]["abstained"] is True
    assert run["trace"]
    assert api.get(f"/runs/{run['run_id']}").json()["data"]["run_id"] == run["run_id"]
    assert api.get("/runs/missing").status_code == 404


def test_workflow_defaults_to_qwen_and_rejects_deepseek(tmp_path):
    api, _ = client(tmp_path)
    accepted = api.post("/workflows/medical-answer/runs", json={"inputs": {"question": "高血压"}})
    assert accepted.status_code == 200
    rejected = api.post("/workflows/medical-answer/runs", json={"inputs": {
        "question": "高血压", "provider": "deepseek"}})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "INVALID_PROVIDER"


def test_model_rate_limit_stays_safe_in_both_answer_routes(tmp_path):
    class LimitedTransport:
        def complete(self, provider, system, user):
            raise ApiError("MODEL_RATE_LIMITED", "生成模型请求过于频繁，请稍后重试", 429)

    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(LimitedTransport()), workflow_root=tmp_path)
    api = TestClient(create_app(service=service))
    uploaded = api.post("/documents", files={"file": (
        "guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())})
    assert uploaded.status_code == 200
    answered = api.post("/answers", json={"question": "高血压", "provider": "qwen"})
    assert answered.status_code == 429
    assert answered.json()["error"]["code"] == "MODEL_RATE_LIMITED"
    run = api.post("/workflows/medical-answer/runs", json={"inputs": {
        "question": "高血压", "provider": "qwen", "top_k": 5}})
    assert run.status_code == 200
    assert run.json()["data"]["status"] == "failed"
    failure = next(item["error"] for item in run.json()["data"]["trace"]
                   if item["status"] == "failed")
    assert failure == {"code": "MODEL_RATE_LIMITED", "message": "生成模型请求过于频繁，请稍后重试"}


@pytest.mark.parametrize("route", ["/answers", "/workflows/medical-answer/runs"])
def test_qwen_answer_routes_disable_default_thinking_before_waiting_for_json(tmp_path, route):
    """Exercise both answer routes through the real SDK's HTTP request body."""
    requests = []

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        # Qwen defaults to thinking, which can exceed the synchronous timeout.
        if body.get("enable_thinking", True):
            raise httpx.ReadTimeout("Thinking exceeded the read timeout", request=request)
        block = json.loads(body["messages"][-1]["content"])["evidence"][0]
        content = json.dumps({"claims": [{
            "text": "应规律随访",
            "citations": [{"chunk_id": block["chunk_id"], "excerpt": "规律随访"}],
        }]})
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
        })

    settings = Settings(_env_file=None, qwen_api_key="test-key",
                        qwen_base_url="https://qwen.invalid/v1")
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http_client:
        def client_factory(**kwargs):
            return OpenAI(**kwargs, http_client=http_client)

        generator = GroundedGenerator(OpenAITransport(settings, client_factory=client_factory))
        service = RagService(settings, MemoryStore(), Embedder(), Reranker(),
                             generator, workflow_root=tmp_path)
        with TestClient(create_app(service=service)) as api:
            uploaded = api.post("/documents", files={"file": (
                "guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())})
            assert uploaded.status_code == 200
            inputs = {"question": "高血压", "provider": "qwen", "top_k": 5}
            response = api.post(route, json={"inputs": inputs} if route.endswith("/runs") else inputs)

    assert response.status_code == 200
    data = response.json()["data"]
    if route.endswith("/runs"):
        assert data["status"] == "success"
        assert all(node["status"] == "success" for node in data["trace"])
        data = data["output"]
    assert data["abstained"] is False
    assert data["answer"].startswith("应规律随访 [")
    assert data["citations"][0]["excerpt"] == "规律随访"
    assert len(requests) == 1
    assert requests[0]["enable_thinking"] is False


def test_invalid_workflow_and_inputs_have_stable_errors(tmp_path):
    api, service = client(tmp_path)
    response = api.post("/workflows/medical-answer/runs", json={"inputs": {}})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_QUERY"
    assert api.get("/workflows/missing").status_code == 404
    definition = api.get("/workflows/medical-answer").json()["data"]
    definition["status"] = "draft"
    definition["nodes"] = []
    invalid = api.post("/workflows/medical-answer/validate", json=definition)
    assert invalid.json()["data"]["valid"] is False


def test_legacy_routes_use_default_workflows(tmp_path):
    api, service = client(tmp_path)
    uploaded = api.post("/documents", files={"file": ("guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())})
    assert uploaded.json()["data"]["duplicate"] is False
    searched = api.post("/search", json={"question": "高血压"})
    assert searched.json()["data"][0]["chunk"]["filename"] == "guide.md"
    answered = api.post("/answers", json={"question": "高血压", "provider": "qwen"})
    assert answered.json()["data"]["abstained"] is False
    assert service.last_workflow_id == "medical-answer"


def test_editor_can_save_draft_then_publish_valid_new_version(tmp_path):
    api, service = client(tmp_path)
    draft = api.get("/workflows/medical-answer").json()["data"]
    next_version = draft["version"] + 1
    draft["version"] = next_version
    draft["status"] = "draft"
    draft["nodes"] = []
    draft["edges"] = []
    saved = api.put("/workflows/medical-answer", json=draft)
    assert saved.status_code == 200
    assert saved.json()["data"]["version"] == next_version
    # A draft never removes the last published version from chat.
    assert api.post("/workflows/medical-answer/runs", json={"inputs": {"question": "高血压"}}).status_code == 200
    draft["version"] = next_version + 1
    draft["status"] = "published"
    assert api.put("/workflows/medical-answer", json=draft).status_code == 422


def test_validation_rejects_bad_node_config(tmp_path):
    api, service = client(tmp_path)
    graph = api.get("/workflows/medical-answer").json()["data"]
    next(node for node in graph["nodes"] if node["type"] == "retrieval.rrf")["config"]["k"] = 0
    result = api.post("/workflows/medical-answer/validate", json=graph)
    assert result.json()["data"]["valid"] is False
    assert "INVALID_CONFIG" in {issue["code"] for issue in result.json()["data"]["issues"]}


def test_published_answer_cannot_bypass_evidence_or_citation(tmp_path):
    api, service = client(tmp_path)
    graph = api.get("/workflows/medical-answer").json()["data"]
    graph["version"] = 2
    graph["edges"] = [edge for edge in graph["edges"] if edge["target"] != "gate"]
    graph["nodes"] = [node for node in graph["nodes"] if node["id"] != "gate"]
    graph["edges"].append({"id": "unsafe", "source": "rerank", "source_port": "evidence",
                            "target": "llm", "target_port": "evidence"})
    result = api.post("/workflows/medical-answer/validate", json=graph).json()["data"]
    assert "UNSAFE_ANSWER_FLOW" in {issue["code"] for issue in result["issues"]}
    assert api.put("/workflows/medical-answer", json=graph).status_code == 422
