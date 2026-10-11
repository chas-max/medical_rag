"""Workflow-backed RAG service with legacy HTTP-compatible methods."""

import asyncio
from pathlib import Path

from .config import Settings
from .types import ApiError
from .workflow.bootstrap import build_registry
from .workflow.contracts import CAPABILITY_WEB_SEARCH, StreamEmitter
from .workflow.definition import WorkflowDefinition
from .workflow.engine import WorkflowEngine, WorkflowRunResult
from .workflow.repository import WorkflowRepository
from .workflow.run_schema import run_inputs_schema
from .workflow.validator import validate_workflow


DEFAULT_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"

# 运行入参不合法时由输入节点抛出这些码，HTTP 层据此回 422。字段本身不再按
# 工作流 ID 维护白名单：图里连了什么输入节点，就按那个节点的声明校验。
REQUEST_ERROR_CODES = frozenset({"INVALID_QUERY", "INVALID_PROVIDER",
                                 "INVALID_DOCUMENT", "INVALID_SOURCE"})


def failed_error(result: WorkflowRunResult) -> dict[str, str]:
    """The first node failure recorded in a run, already sanitized by the engine."""
    return next((item.error for item in result.trace if item.status == "failed"), None) or {}


def global_capabilities(generator: object) -> dict[str, bool]:
    """Capabilities every workflow gets by construction, independent of its graph."""
    # 生成器只有在 Tavily 已配置时才持有工具；没有钥匙就没有联网能力，与不联网时行为一致。
    return {CAPABILITY_WEB_SEARCH: bool(getattr(generator, "tools", None))}


class RagService:
    def __init__(self, settings: Settings, store: object, embedder: object,
                 reranker: object, generator: object,
                 workflow_root: Path | None = None) -> None:
        self.settings = settings
        self.store = store
        self.registry = build_registry(store, embedder, reranker, generator, settings)
        self.capabilities = global_capabilities(generator)
        self.engine = WorkflowEngine(self.registry, self.capabilities)
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

    async def run_workflow(self, workflow_id: str, inputs: dict[str, object],
                           emit: StreamEmitter | None = None) -> WorkflowRunResult:
        try:
            definition = self.repository.get_published(workflow_id)
        except KeyError as exc:
            raise ApiError("WORKFLOW_NOT_PUBLISHED", "仅可运行已发布的工作流", 409) from exc
        if validate_workflow(definition, self.registry):
            raise ApiError("INVALID_WORKFLOW", "工作流校验未通过", 422)
        self.last_workflow_id = workflow_id
        result = await self.engine.run(definition, inputs, emit=emit)
        self.runs[result.run_id] = result
        if result.status == "failed":
            error = failed_error(result)
            if error.get("code") in REQUEST_ERROR_CODES:
                raise ApiError(error["code"], error.get("message", "请求参数不符合要求"), 422)
        return result

    def run(self, workflow_id: str, inputs: dict[str, object]) -> dict:
        result = asyncio.run(self.run_workflow(workflow_id, inputs))
        if result.status == "failed":
            error = failed_error(result)
            code = error.get("code", "DEPENDENCY_ERROR")
            if code == "NODE_EXECUTION_ERROR":
                code = "DEPENDENCY_ERROR"
            status = {"MODEL_RATE_LIMITED": 429, "MODEL_UPSTREAM_ERROR": 502}.get(code, 503)
            raise ApiError(code, error.get("message", "检索或模型服务不可用"), status)
        return result.output

    def run_inputs_schema(self, workflow_id: str) -> list[dict[str, object]]:
        """Derive the run inputs from the graph instead of a hardcoded field list."""
        definition = self.workflow(workflow_id)
        return run_inputs_schema(definition, self.registry)

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
