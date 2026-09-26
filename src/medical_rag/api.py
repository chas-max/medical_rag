"""FastAPI entry point and consistent public response envelope."""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings
from .types import ApiError


class SearchRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


class AnswerRequest(SearchRequest):
    provider: Literal["deepseek", "qwen"] = "deepseek"


def _success(value: object) -> dict:
    return {"success": True, "data": jsonable_encoder(value), "error": None}


def _failure(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "data": None, "error": {"code": code, "message": message}})


def create_app(service: object | None = None) -> FastAPI:
    settings = Settings()
    active_service = service

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal active_service
        if active_service is None:
            from pymilvus import MilvusClient

            from .embeddings import BgeEmbedder, BgeReranker
            from .generation import GroundedGenerator, OpenAITransport
            from .service import RagService
            from .store import MilvusStore

            client = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or None)
            store = MilvusStore(client, settings.collection_name)
            store.ensure_schema()
            active_service = RagService(settings, store, BgeEmbedder(), BgeReranker(),
                                        GroundedGenerator(OpenAITransport(settings)))
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

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, error: ValueError):
        return _failure("VALIDATION_ERROR", str(error), 422)

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
        return _success(_service().ingest(filename, content, source_id))

    @app.post("/search")
    def search(payload: SearchRequest):
        return _success(_service().search(payload.question, payload.top_k))

    @app.post("/answers")
    def answer(payload: AnswerRequest):
        return _success(_service().answer(payload.question, payload.provider, payload.top_k))

    return app


app = create_app()
