import json
from pathlib import Path

import httpx
import pytest
from document_helpers import upload_and_process
from fastapi.testclient import TestClient
from openai import OpenAI
from test_service import Embedder, MemoryStore, Reranker, Transport

from medical_rag.api import create_app
from medical_rag.config import Settings
from medical_rag.generation import GroundedGenerator, OpenAITransport
from medical_rag.service import RagService
from medical_rag.types import ApiError


def client(tmp_path: Path):
    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(Transport()), workflow_root=tmp_path)
    return TestClient(create_app(service=service)), service


def test_answer_route_reports_model_rate_limit(tmp_path):
    class LimitedTransport:
        def complete(self, provider, system, user):
            raise ApiError("MODEL_RATE_LIMITED", "生成模型请求过于频繁，请稍后重试", 429)

    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(LimitedTransport()), workflow_root=tmp_path)
    api = TestClient(create_app(service=service))
    uploaded = upload_and_process(api, files={"file": (
        "guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())})
    assert uploaded["status"] == "succeeded"
    answered = api.post("/answers", json={"question": "高血压", "provider": "qwen"})
    assert answered.status_code == 429
    assert answered.json()["error"]["code"] == "MODEL_RATE_LIMITED"


@pytest.mark.parametrize("route", ["/answers"])
def test_qwen_answer_routes_disable_default_thinking_before_waiting_for_json(tmp_path, route):
    """Exercise both answer routes through the real SDK's HTTP request body."""
    requests = []

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        # Qwen defaults to thinking, which can exceed the synchronous timeout.
        if body.get("enable_thinking", True):
            raise httpx.ReadTimeout("Thinking exceeded the read timeout", request=request)
        payload = json.loads(body["messages"][-1]["content"])
        if "evidence" not in payload:
            # 回答规划节点的调用只看结构与主题。
            content = json.dumps({"sections": ["基本概念解释", "常见处理方式"]}, ensure_ascii=False)
        else:
            block = payload["evidence"][0]
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
            uploaded = upload_and_process(api, files={"file": (
                "guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())})
            assert uploaded["status"] == "succeeded"
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
    # medical-answer v5：HyDE、纠错重写、回答规划与生成本体各一次，均关闭思维链。
    assert len(requests) == 4
    assert all(item["enable_thinking"] is False for item in requests)


def test_legacy_routes_use_default_workflows(tmp_path):
    api, service = client(tmp_path)
    uploaded = upload_and_process(api, files={"file": ("guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())})
    assert uploaded["result"]["duplicate"] is False
    searched = api.post("/search", json={"question": "高血压"})
    assert searched.json()["data"][0]["chunk"]["filename"] == "guide.md"
    answered = api.post("/answers", json={"question": "高血压", "provider": "qwen"})
    assert answered.json()["data"]["abstained"] is False
    assert service.last_workflow_id == "medical-answer"
