"""FastAPI entry point and consistent public response envelope."""

import json
import threading
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from loguru import logger
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .chat_api import chat_router
from .chat_history import ChatHistoryStore, HistoryImport
from .chat_runtime import ChatRuntime
from .config import Settings
from .dialogue_models import DialogueRun, DialogueSnapshot
from .jobs import DocumentJobStore
from .memory import MemoryStore
from .memory_api import memory_router
from .public_errors import public_error, public_http_error
from .settings_store import ModelSettings, SettingsStore
from .sse import StreamEmitter, bridge
from .types import ApiError

# The bounded snapshot is already small by construction; this is the hard stop for
# any caller that sends a hand-built body instead.
MAX_DIALOGUE_CONTEXT_BYTES = 64 * 1024

# Only these may be produced by a workflow node. ``done`` and ``error`` belong to
# the transport, so an upstream node can never forge a terminal frame.
ANSWER_STREAM_EVENTS = frozenset({"status", "claim", "analysis"})


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
    # The patient portal must not receive the single-user legacy memory/profile.
    memory: bool = True
    # Internal backend extension: a trusted caller may forward this conversation's
    # bounded snapshot. The browser never needs to send it and cannot grant access
    # with it; the payload carries no credentials.
    dialogue_context: DialogueSnapshot | None = None

    @field_validator("dialogue_context", mode="before")
    @classmethod
    def parse_context(cls, value: object) -> object:
        """Validate a JSON body through the strict JSON path, not Python tuples.

        FastAPI hands the parsed body to ``model_validate``, and a strict model's
        tuple fields would then reject the arrays a real JSON request contains.
        """
        if value is None or isinstance(value, DialogueSnapshot):
            return value
        try:
            return DialogueSnapshot.model_validate_json(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid dialogue context") from error


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


def answer_stream_failure(error: Exception) -> dict:
    """Map a failed streaming run to a catalogue error, never to upstream text.

    Only the exception code is used as a lookup key; its message and claimed
    status are discarded, matching the non-streaming error handler.
    """
    safe = public_error(error.code if isinstance(error, ApiError) else None)
    return {**safe.data(), "status": safe.status}


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
            embedder = BgeEmbedder()
            history = None
            if settings.chat_recall_enabled:
                from .conversation_index import ConversationHistory

                history = ConversationHistory(client, settings.chat_turn_collection,
                                              embed=embedder.encode,
                                              top_k=settings.chat_recall_top_k,
                                              scope=settings.chat_recall_scope,
                                              half_life_days=settings.chat_recall_half_life_days)
                try:
                    history.ensure_schema()
                except Exception:  # noqa: BLE001 - recall is optional, answering is not
                    logger.warning("会话向量集合不可用，历史召回已关闭")
                    history = None
            active_service = RagService(settings, store, embedder, BgeReranker(),
                                        build_generator(settings),
                                        conversation_history=history)
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
    # The rolling digest rides the configured endpoint; with no key there is nothing to call.
    from .generation import OpenAITransport
    from .summarize import ConversationSummarizer

    summarizer = ConversationSummarizer(
        history, OpenAITransport(settings) if settings.qwen_api_key else None)

    def _conversation_history():
        """Resolve the per-session turn index lazily; None keeps recall disabled."""
        try:
            return getattr(_service(), "conversation_history", None)
        except Exception:  # noqa: BLE001 - optional index, never a request failure
            return None

    from .conversation_index import ConversationIndexer

    indexer = (ConversationIndexer(_conversation_history)
               if settings.chat_recall_enabled else None)
    runtime = ChatRuntime(history, memory, profile_analyzer, _service,
                          summarizer=summarizer, indexer=indexer,
                          dialogue_state_enabled=settings.dialogue_state_enabled)
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

    @app.delete("/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str):
        """Remove one chat together with its digest, vectors and episode copies."""
        return _success(runtime.delete_conversation(conversation_id))

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

    def answer_options(payload: AnswerRequest) -> dict[str, object]:
        """Validate the request and resolve memory/dialogue before any work starts.

        Shared by the plain and the streaming handler so a combination one route
        refuses can never be accepted by the other.
        """
        if payload.dialogue_context is not None and payload.memory:
            raise ApiError("INVALID_QUERY", "上下文参数组合无效", 422)
        run = (DialogueRun(payload.dialogue_context or DialogueSnapshot())
               if settings.dialogue_state_enabled else None)
        if payload.dialogue_context is not None and not settings.dialogue_state_enabled:
            # Never silently drop the context and answer as if the follow-up were
            # standalone: the caller would commit a state the server never used.
            raise ApiError("DIALOGUE_FEATURE_DISABLED", "连续追问功能未启用", 409)
        if run is not None and payload.dialogue_context is not None:
            size = len(payload.dialogue_context.model_dump_json().encode("utf-8"))
            if size > MAX_DIALOGUE_CONTEXT_BYTES:
                raise ApiError("INVALID_QUERY", "上下文参数组合无效", 422)
        # Memory reads stay exactly as before; a context call never touches them.
        options: dict[str, object] = {"dialogue": run, "retain_run": payload.memory}
        if payload.memory:
            _, context = memory_snapshot(payload.question)
            if context:
                options["memory_context"] = context
        return options

    def with_resolution(answer: dict, payload: AnswerRequest,
                        options: dict[str, object]) -> dict:
        """Attach this run's verified resolution for the internal backend only."""
        run = options.get("dialogue")
        if (payload.dialogue_context is not None and run is not None
                and getattr(run, "resolution", None) is not None):
            return {**answer, "dialogue_resolution": run.resolution.model_dump(mode="json")}
        return answer

    @app.post("/answers")
    def answer(payload: AnswerRequest):
        options = answer_options(payload)
        result = _service().answer(payload.question, payload.provider,
                                   payload.top_k or settings.top_k, **options)
        return _success(with_resolution(result, payload, options))

    @app.post("/answers/stream")
    async def answer_stream(payload: AnswerRequest):
        """Same run and validation as ``/answers``, publishing intermediate events.

        Request errors are raised before the response starts, so they keep the
        normal JSON envelope and status code; only in-run failures become an
        ``error`` frame, because the status line is already sent by then.

        This handler must stay ``async``: the bridge registers callbacks on the
        running event loop, which a sync handler (executed in a worker thread)
        would not have.
        """
        options = await run_in_threadpool(answer_options, payload)

        def execute(emit: StreamEmitter) -> dict:
            return with_resolution(
                _service().answer(payload.question, payload.provider,
                                  payload.top_k or settings.top_k, emit=emit, **options),
                payload, options)

        return bridge(execute, ANSWER_STREAM_EVENTS, failure=answer_stream_failure,
                      frame=_event_frame)


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
