"""Workflow-backed RAG service with legacy HTTP-compatible methods."""

import asyncio
from pathlib import Path

from .config import Settings
from .types import ApiError
from .workflow.bootstrap import build_registry
from .workflow.definition import WorkflowDefinition
from .workflow.engine import WorkflowEngine, WorkflowRunResult
from .workflow.repository import WorkflowRepository
from .workflow.validator import validate_workflow


DEFAULT_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


class RagService:
    def __init__(self, settings: Settings, store: object, embedder: object,
                 reranker: object, generator: object,
                 workflow_root: Path | None = None) -> None:
        self.settings = settings
        self.store = store
        self.registry = build_registry(store, embedder, reranker, generator, settings)
        self.engine = WorkflowEngine(self.registry)
        self.repository = WorkflowRepository(workflow_root or DEFAULT_WORKFLOWS,
                                             DEFAULT_WORKFLOWS if workflow_root else None)
        self.runs: dict[str, WorkflowRunResult] = {}
        self.last_workflow_id: str | None = None

    def nodes(self) -> list[dict]:
        return self.registry.metadata()

    def workflows(self) -> list[WorkflowDefinition]:
        return self.repository.list_published()

    def workflow(self, workflow_id: str) -> WorkflowDefinition:
        try:
            return self.repository.get(workflow_id)
        except KeyError as exc:
            raise ApiError("WORKFLOW_NOT_FOUND", "工作流不存在", 404) from exc
        except ValueError as exc:
            raise ApiError("INVALID_WORKFLOW", "工作流定义无效", 422) from exc

    def validate_workflow(self, definition: WorkflowDefinition) -> dict:
        issues = validate_workflow(definition, self.registry)
        return {"valid": not issues, "issues": issues}

    def save_workflow(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        issues = validate_workflow(definition, self.registry)
        if issues and definition.status != "draft":
            raise ApiError("INVALID_WORKFLOW", "工作流校验未通过", 422)
        try:
            return self.repository.save(definition)
        except ValueError as exc:
            raise ApiError("WORKFLOW_CONFLICT", str(exc), 409) from exc

    async def run_workflow(self, workflow_id: str, inputs: dict[str, object]) -> WorkflowRunResult:
        try:
            definition = self.repository.get_published(workflow_id)
        except KeyError as exc:
            raise ApiError("WORKFLOW_NOT_PUBLISHED", "仅可运行已发布的工作流", 409) from exc
        if validate_workflow(definition, self.registry):
            raise ApiError("INVALID_WORKFLOW", "工作流校验未通过", 422)
        if workflow_id in ("medical-answer", "medical-search", "medical-answer-expanded"):
            if not isinstance(inputs.get("question"), str) or not inputs["question"].strip():
                raise ApiError("INVALID_QUERY", "问题不能为空", 422)
            top_k = inputs.get("top_k", self.settings.top_k)
            if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
                raise ApiError("INVALID_QUERY", "top_k 必须在 1 到 20 之间", 422)
            if inputs.get("provider", "qwen") != "qwen":
                raise ApiError("INVALID_PROVIDER", "不支持的生成模型厂商", 422)
            inputs = {**inputs, "top_k": top_k}
        self.last_workflow_id = workflow_id
        result = await self.engine.run(definition, inputs)
        self.runs[result.run_id] = result
        return result

    def run(self, workflow_id: str, inputs: dict[str, object]) -> dict:
        result = asyncio.run(self.run_workflow(workflow_id, inputs))
        if result.status == "failed":
            error = next((item.error for item in result.trace if item.status == "failed"), None) or {}
            code = error.get("code", "DEPENDENCY_ERROR")
            if code == "NODE_EXECUTION_ERROR":
                code = "DEPENDENCY_ERROR"
            status = {"MODEL_RATE_LIMITED": 429, "MODEL_UPSTREAM_ERROR": 502}.get(
                code, 422 if code in ("INVALID_DOCUMENT", "INVALID_SOURCE", "INVALID_QUERY", "INVALID_PROVIDER") else 503
            )
            raise ApiError(code, error.get("message", "检索或模型服务不可用"), status)
        return result.output

    def get_run(self, run_id: str) -> WorkflowRunResult:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise ApiError("RUN_NOT_FOUND", "运行记录不存在", 404) from exc

    def ingest(self, filename: str, content: bytes, source_id: str | None = None) -> dict:
        return self.run("document-ingestion", {"filename": filename, "content": content,
                                               "source_id": source_id})

    def list_documents(self) -> list:
        return self.store.list_documents()

    def search(self, question: str, top_k: int) -> list:
        return self.run("medical-search", {"question": question, "top_k": top_k})["evidence"]

    def answer(self, question: str, provider: str, top_k: int) -> dict:
        return self.run("medical-answer", {"question": question, "provider": provider,
                                           "top_k": top_k})

    def health(self) -> dict:
        return {"milvus": self.store.ping()}
