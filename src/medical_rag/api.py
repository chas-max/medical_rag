"""FastAPI entry point and consistent public response envelope."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .types import ApiError
from .workflow.definition import WorkflowDefinition, WorkflowRunRequest


class SearchRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=20)


class AnswerRequest(SearchRequest):
    provider: Literal["qwen"] = "qwen"


def _success(value: object) -> dict:
    return {"success": True, "data": jsonable_encoder(value), "error": None}


def _failure(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "data": None, "error": {"code": code, "message": message}})


def create_app(service: object | None = None, frontend_dist: Path | None = None) -> FastAPI:
    settings = Settings()
    active_service = service

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal active_service
        if active_service is None:
            from pymilvus import MilvusClient

            from .embeddings import BgeEmbedder, BgeReranker
            from .generation import build_generator
            from .service import RagService
            from .store import MilvusStore

            client = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or None)
            store = MilvusStore(client, settings.collection_name)
            store.ensure_schema()
            active_service = RagService(settings, store, BgeEmbedder(), BgeReranker(),
                                        build_generator(settings))
        yield

    app = FastAPI(title="公开医疗资料检索问答", version="0.1.0", lifespan=lifespan)

    def _service():
        if active_service is None:
            raise ApiError("NOT_READY", "服务尚未初始化", 503)
        return active_service

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError):
        return _failure(error.code, error.message, error.status)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError):
        return _failure("VALIDATION_ERROR", "请求参数不符合要求", 422)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, error: StarletteHTTPException):
        return _failure("HTTP_ERROR", str(error.detail), error.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception):
        return _failure("DEPENDENCY_ERROR", "检索或模型服务不可用", 503)

    @app.get("/health")
    def health():
        return _success(_service().health())

    @app.get("/documents")
    def documents():
        return _success(_service().list_documents())

    @app.post("/documents")
    async def upload(file: UploadFile = File(...), source_id: str | None = Form(None)):
        filename = file.filename or ""
        if not filename.lower().endswith((".md", ".txt", ".pdf")):
            raise ApiError("UNSUPPORTED_FORMAT", "仅支持 Markdown、TXT 和可提取文字的 PDF", 415)
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise ApiError("FILE_TOO_LARGE", "文件超过上传大小限制", 413)
        return _success(await run_in_threadpool(_service().ingest, filename, content, source_id))

    @app.post("/search")
    def search(payload: SearchRequest):
        return _success(_service().search(payload.question, payload.top_k or settings.top_k))

    @app.post("/answers")
    def answer(payload: AnswerRequest):
        return _success(_service().answer(payload.question, payload.provider, payload.top_k or settings.top_k))

    @app.get("/nodes")
    def nodes():
        return _success(_service().nodes())

    @app.get("/workflows")
    def workflows():
        return _success(_service().workflows())

    @app.post("/workflows")
    def create_workflow(definition: WorkflowDefinition):
        return _success(_service().save_workflow(definition))

    @app.get("/workflows/{workflow_id}")
    def workflow(workflow_id: str):
        return _success(_service().workflow(workflow_id))

    @app.put("/workflows/{workflow_id}")
    def update_workflow(workflow_id: str, definition: WorkflowDefinition):
        if definition.id != workflow_id:
            raise ApiError("INVALID_WORKFLOW", "工作流 ID 与路径不一致", 422)
        return _success(_service().save_workflow(definition))

    @app.post("/workflows/{workflow_id}/validate")
    def validate_workflow(workflow_id: str, definition: WorkflowDefinition):
        if definition.id != workflow_id:
            raise ApiError("INVALID_WORKFLOW", "工作流 ID 与路径不一致", 422)
        return _success(_service().validate_workflow(definition))

    @app.post("/workflows/{workflow_id}/runs")
    async def run_workflow(workflow_id: str, payload: WorkflowRunRequest):
        return _success(await _service().run_workflow(workflow_id, payload.inputs))

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        return _success(_service().get_run(run_id))

    dist = frontend_dist or Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if (dist / "index.html").is_file():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/ui/assets", StaticFiles(directory=assets), name="frontend-assets")

        @app.get("/ui")
        def frontend_home():
            return RedirectResponse("/ui/chat")

        @app.get("/ui/{path:path}")
        def frontend_page(path: str):
            if "." in path.rsplit("/", 1)[-1]:
                raise ApiError("NOT_FOUND", "前端资源不存在", 404)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
