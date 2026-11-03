"""FastAPI entry point and consistent public response envelope."""

import json
import logging
import threading
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .chat_api import chat_router
from .chat_history import ChatHistoryStore, HistoryImport
from .chat_runtime import ChatRuntime
from .config import Settings
from .jobs import DocumentJobStore
from .memory import MemoryStore
from .memory_api import memory_router
from .public_errors import public_error, public_http_error
from .settings_store import ModelSettings, SettingsStore
from .types import ApiError

logger = logging.getLogger(__name__)


class _RejectWebSockets:
    """No WebSocket path is authorized until the full security model exists."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


def _loopback_authority(value: str) -> bool:
    """Accept only literal loopback hosts, not DNS names that can rebind."""
    try:
        url = urlsplit(f"//{value}")
        if url.username or url.password or url.path or url.query or url.fragment:
            return False
        _ = url.port  # Validate an optional port.
        host = url.hostname or ""
        return host == "localhost" or ip_address(host).is_loopback
    except ValueError:
        return False


def _loopback_origin(value: str) -> bool:
    try:
        url = urlsplit(value)
        return (url.scheme in {"http", "https"} and not url.path and not url.query
                and not url.fragment and _loopback_authority(url.netloc))
    except ValueError:
        return False


class SearchRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=20)


class AnswerRequest(SearchRequest):
    # A record label only: one endpoint is configured at a time, so any name is accepted.
    provider: str = "qwen"


class ModelConfigRequest(BaseModel):
    """Plain strings on purpose: an incomplete form gets its own error code, not a 422 about schemas."""

    model_name: str = ""
    base_url: str = ""
    api_key: str = ""


class ModelProfileRequest(BaseModel):
    profile_id: str = ""


def _success(value: object) -> dict:
    return {"success": True, "data": jsonable_encoder(value), "error": None}


def _failure(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "data": None, "error": {"code": code, "message": message}})


def _event_frame(event: dict) -> str:
    """Encode one stream event as a Server-Sent Events frame."""
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"


def create_app(service: object | None = None, frontend_dist: Path | None = None,
               settings_store: SettingsStore | None = None,
               profile_analyzer: object | None = None) -> FastAPI:
    settings = Settings()
    active_service = service
    service_lock = threading.Lock()
    model_settings = ModelSettings(settings, settings_store)
    model_settings.apply_saved()
    history = ChatHistoryStore(settings.chat_history_file)
    memory = MemoryStore(settings.chat_history_file, settings.memory_episodes_dir)
    document_jobs = DocumentJobStore(settings, repository=getattr(service, "repository", None))

    def _service():
        nonlocal active_service
        if active_service is not None:
            return active_service
        with service_lock:
            if active_service is not None:
                return active_service
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
        return active_service

    app = FastAPI(title="公开医疗资料检索问答", version="0.1.0")
    app.add_middleware(_RejectWebSockets)
    if settings.access_mode == "unsafe-local-demo":
        logger.warning("Unsafe local demo mode enabled: no authentication or patient isolation; never expose this service")

    @app.middleware("http")
    async def security_lockdown(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if settings.access_mode == "locked":
            if request.method == "GET" and request.url.path == "/health":
                return JSONResponse(_success({"status": "security_not_ready"}),
                                    headers={"Cache-Control": "no-store"})
            return _failure("SECURITY_NOT_READY", "安全能力尚未就绪，接口已关闭", 503)
        try:
            local_peer = bool(request.client and ip_address(request.client.host).is_loopback)
        except ValueError:
            local_peer = False
        forwarded = any(
            name in {"forwarded", "x-real-ip"} or name.startswith("x-forwarded-")
            for name in request.headers
        )
        hosts = request.headers.getlist("host")
        origins = request.headers.getlist("origin")
        bad_host = len(hosts) != 1 or not _loopback_authority(hosts[0])
        bad_origin = (len(origins) > 1 or (origins and not _loopback_origin(origins[0]))
                      or (not origins and request.method not in {"GET", "HEAD", "OPTIONS"}))
        if not local_peer or forwarded or bad_host or bad_origin:
            return _failure("LOCAL_DEMO_ONLY", "本机演示模式禁止远程或代理访问", 403)
        return await call_next(request)

    app.state.memory = memory
    app.state.memory_worker = None
    app.state.document_jobs = document_jobs
    app.include_router(memory_router(memory))
    if profile_analyzer is None:
        from .generation import OpenAITransport
        from .profile_intent import ProfileIntentAnalyzer

        profile_analyzer = ProfileIntentAnalyzer(OpenAITransport(settings))
    runtime = ChatRuntime(history, memory, profile_analyzer, _service)
    app.state.chat_runtime = runtime
    app.include_router(chat_router(runtime, _success, _event_frame))

    def memory_snapshot(question: object) -> tuple[int | None, str]:
        try:
            return memory.snapshot(question) if isinstance(question, str) else (None, "")
        except (ApiError, ValueError):
            # The authoritative answer is independent of the optional memory store.
            return None, ""

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError):
        safe_error = public_error(error.code)
        return _failure(safe_error.code, safe_error.message, safe_error.status)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError):
        return _failure("VALIDATION_ERROR", "请求参数不符合要求", 422)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, error: StarletteHTTPException):
        safe_error = public_http_error(error.status_code)
        return _failure(safe_error.code, safe_error.message, safe_error.status)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception):
        return _failure("DEPENDENCY_ERROR", "检索或模型服务不可用", 503)

    @app.get("/health")
    def health():
        data = (active_service.health() if active_service is not None else
                {"status": "uninitialized", "milvus": False})
        try:
            messaging = document_jobs.health()
        except ApiError:
            # Optional background storage cannot change primary liveness semantics.
            messaging = {"status": "degraded", "error": "JOB_STORAGE_ERROR"}
        return _success({**data, "messaging": messaging})

    @app.get("/conversations")
    def conversations():
        return _success(history.list_conversations())

    @app.post("/conversations/import")
    def import_conversations(payload: HistoryImport):
        return _success(history.import_conversations(payload.conversations))

    @app.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str):
        return _success(history.get_conversation(conversation_id))

    @app.get("/settings/model")
    def model_config():
        return _success(model_settings.current())

    @app.post("/settings/model")
    async def confirm_model_config(payload: ModelConfigRequest):
        # Confirmation probes the endpoint first, so it runs off the event loop like the
        # workflow routes rather than stalling every other request for up to 20 seconds.
        return _success(await run_in_threadpool(model_settings.confirm, payload.model_name,
                                                 payload.base_url, payload.api_key))

    @app.post("/settings/model/select")
    def select_model_profile(payload: ModelProfileRequest):
        # Switching a stored profile touches no network: it was proven when confirmed.
        return _success(model_settings.select(payload.profile_id))

    @app.delete("/settings/model/{profile_id}")
    def delete_model_profile(profile_id: str):
        return _success(model_settings.remove(profile_id))

    @app.get("/documents")
    def documents():
        return _success(_service().list_documents())

    @app.post("/documents", status_code=202)
    async def upload(file: Annotated[UploadFile, File()],
                     source_id: Annotated[str | None, Form()] = None):
        filename = file.filename or ""
        if not filename.lower().endswith((".md", ".txt", ".pdf")):
            raise ApiError("UNSUPPORTED_FORMAT", "仅支持 Markdown、TXT 和可提取文字的 PDF", 415)
        content = await file.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            raise ApiError("FILE_TOO_LARGE", "文件超过上传大小限制", 413)
        return _success(await run_in_threadpool(document_jobs.submit, filename, content, source_id))

    @app.get("/document-jobs")
    def list_document_jobs(active: bool = False, limit: int = Query(default=50, ge=1, le=100)):
        return _success(document_jobs.list_jobs(active=active, limit=limit))

    @app.get("/document-jobs/{job_id}")
    def get_document_job(job_id: str):
        return _success(document_jobs.get(job_id))

    @app.post("/search")
    def search(payload: SearchRequest):
        return _success(_service().search(payload.question, payload.top_k or settings.top_k))

    @app.get("/chunks/{chunk_id}")
    def chunk(chunk_id: str):
        """Citation source lookup: the stored block text for on-demand display."""
        found = _service().get_chunk(chunk_id)
        if found is None:
            # Web evidence blocks are per-run and intentionally not persisted;
            # callers fall back to the citation's source link.
            raise ApiError("CHUNK_NOT_FOUND", "未找到该资料块", 404)
        return _success(found)

    @app.post("/answers")
    def answer(payload: AnswerRequest):
        _, context = memory_snapshot(payload.question)
        options = {"memory_context": context} if context else {}
        return _success(_service().answer(payload.question, payload.provider, payload.top_k or settings.top_k, **options))


    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        saved = history.get_run(run_id)
        return _success(saved if saved is not None else _service().get_run(run_id))

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
