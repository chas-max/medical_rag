# CODEBUDDY.md This file provides guidance to CodeBuddy when working with code in this repository.

## 项目概览

面向公开中文医疗资料的 RAG 问答系统：父子分块 + BGE 向量入库 Milvus，回答走「原问题向量 + BM25 + HyDE」多路召回、RRF 融合、重排、证据门控、答案规划、引用校验。除旧版单用户演示外，另有一条与它完全隔离的 OIDC 患者门户（PostgreSQL + Keycloak）。

**安全状态：未通过生产医疗数据准入。** 旧业务接口与后台 worker 默认锁定，只能处理本机虚构数据；患者门户也未与真实医院身份服务联调。

## 常用命令

### 后端（Python 3.11+）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-user -e ".[dev]"   # 安装（含 pytest/ruff/pymupdf）
python -m ruff check src tests                                     # lint，规则集见 pyproject 的 E4/E7/E9/F
```

```powershell
# 全量测试：排除 integration 与已知失败的 500 题锚点映射用例
python -m pytest -q -m "not integration" --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking

# CI 实际执行的口径（还额外排除需要本地 PDF 的 pinned_release）
python -m pytest -m "not integration and not pinned_release"

# 单个测试
python -m pytest tests/test_service.py::test_name -q
```

`-m "not integration"` 之外的集成测试用独立环境变量逐组开启，缺少 Docker/PostgreSQL 时选中即失败而非退化为 mock：

```powershell
$env:RUN_MILVUS_INTEGRATION='1';   python -m pytest tests/test_milvus_integration.py -q
$env:RUN_POSTGRES_INTEGRATION='1'; python -m pytest tests/test_patient_repository.py tests/test_postgres_isolation.py -q
$env:RUN_GATEWAY_INTEGRATION='1';  python -m pytest tests/test_gateway.py -q
$env:RUN_KEYCLOAK_INTEGRATION='1'; python -m pytest tests/test_keycloak_identity.py -q --basetemp outputs/keycloak-check
```

`tests/conftest.py` 的 autouse fixture 会强制 `MEDICAL_RAG_ACCESS_MODE=unsafe-local-demo` 并把 `TestClient` 的 peer/Host/Origin 设为回环 —— 新增测试默认就在该模式下运行，要测锁定态需自行覆盖环境变量。

### 前端（Node 20.19+ / 22.12+，`frontend/`）

```powershell
npm --prefix frontend install
npm --prefix frontend run dev          # Vite dev，base=/ui/，/api 代理到 127.0.0.1:8001
npm --prefix frontend run lint         # tsc --noEmit && eslint
npm --prefix frontend test             # vitest run
npm --prefix frontend test -- src/test/pages.test.tsx   # 单个前端测试
npm --prefix frontend run build        # 管理端 → frontend/dist
npm --prefix frontend run build:patient # 患者端 → frontend/dist-patient
```

### 启动

```powershell
.\start.ps1                 # 一键：Milvus/RabbitMQ + RAG(8001) + 患者 PG/Keycloak + 门户(8443)
.\start.ps1 -WithWorkers    # 额外启动 relay/ingest/memory
.\start.ps1 -ResetPatient   # 重建患者演示环境
.\start.ps1 -Stop / -StopAll
```

手动启动（三个终端，每个终端都要单独设访问模式）：

```powershell
docker compose up -d
$env:MEDICAL_RAG_ACCESS_MODE='unsafe-local-demo'
python -m medical_rag.api_server --host 127.0.0.1 --port 8001
python -m medical_rag.worker relay      # 另开终端：relay / ingest / memory
python -m medical_rag.security_server --config C:\secure\medical\config.toml   # 患者门户（直接 TLS）
```

端口约定：Milvus 19531、RabbitMQ 5673（管理台 15673）、RAG API 8001、患者门户 8443。

## 架构

### 一、两条互不相通的运行链路

这是理解仓库的首要前提，改动任何一侧都要先确认不会把另一侧的隔离打开：

1. **旧单用户应用** `api.py::create_app()`（模块级 `app`）——FastAPI，提供 `/answers`、`/chat/*`（SSE）、`/search`、`/documents`、`/settings/model`、`/memory/*`，并把 `frontend/dist` 挂在 `/ui`。默认 `access_mode="locked"`，中间件 `security_lockdown` 让除 `GET /health` 外全部返回 503；只有显式 `unsafe-local-demo` 才放行，且要求回环 peer、无转发头、单一回环 Host/Origin。`worker.py::_require_demo_access()` 对三个 worker 做同样检查。
2. **患者门户** `security_server.py` ——独立 FastAPI 进程，`python -m medical_rag.security_server --config <TOML>` 启动，直接 uvicorn TLS。它**不读 `.env`**，只认操作员进程环境与 TOML；启动前 `preflight()` 逐表校验列级权限（只读 SELECT）、RLS 必须 `relrowsecurity AND relforcerowsecurity`、要求 `security_audit_events`/`provider_logout_events`/`support_cases` 等表存在，缺失即 `StartupError`。路由由 `patient_api.create_patient_app()` 组装：`/auth/*`（OIDC 授权码 + S256 PKCE + 回调 + backchannel-logout）、`/me`、`/me/consents/health`、`/conversations[/{id}/turns]`、`/chat`、`/support/*`、`/audit`，只挂载 `frontend/dist-patient/patient.html` 与 `/assets`，不提供 `/ui`、文档、记忆、模型设置。

患者问答通过 `patient_chat.ChatClient` 调本机 RAG 的 `POST /answers`（或 `streaming = true` 时的 `/answers/stream`）；`base_url` 被强制校验为回环，上游异常一律降级为 `CHAT_UNAVAILABLE`。流式链路是四层的：RAG `/answers/stream` → 患者 `POST /chat/stream`（逐帧裁剪为 `status`/`claim`/`done`/`error`）→ 患者网关 → 浏览器 `fetch` 解析；`sse.py` 是唯一的传输桥接，`done` 帧一律在落库之后才发送。数据库迁移在 `database/migrations/0001`–`0009`，只用于新建的隔离库，不迁移旧 SQLite。

### 二、问答/检索链路是一张 JSON 工作流图

**检索逻辑不在 `service.py` 里。** `service.RagService` 只做编排：`answer()` → `run_workflow()` → `WorkflowEngine.run()`，真正的管线由 `workflows/medical-answer.v*.json` 定义。仓库里还有 `medical-search`、`document-ingestion`、`local-retrieval-correction`、`*-agentic/decompose/direct/expanded` 等图。

- **定义与仓库**：`workflow/definition.py` 的 `WorkflowDefinition`（id/version/status=draft|validated|published/kind）；`workflow/repository.py` 按 `workflows/<id>.v<n>.json` 取最高版本或最高已发布版本，保存时原子换文件并更新 `.index.json`。**没有 `/workflows` 之类的 HTTP 编辑/触发面**（测试显式断言 404），工作流只经 `RagService` 被间接运行。
- **执行语义**：`workflow/engine.py` 做 Kahn 拓扑排序（有环即失败），但节点是**顺序执行**的——JSON 里并列的三路检索并不是并发。节点间靠端口值传递，`collect` 端口把多路来源聚成 list（RRF 就是这样拿到 2–3 路结果）。节点可返回 `Halt` 剪掉下游子树（证据门控用它表示"不继续生成"）；缺必填输入是 `skipped`、`activation` 不匹配是 `inactive`，都不算失败；节点异常被压成脱敏的 `NODE_EXECUTION_ERROR`。
- **节点注入**：`workflow/registry.py` 维护节点名→实现，`workflow/bootstrap.py::build_registry()` 用 `functools.partial` 把 store/embedder/reranker/generator/settings 注入节点，节点本身无全局状态。新增能力必须同时改实现、`registry`、`contracts` 里的 `PortSpec`，并在 JSON 里连线；`validator.py` 在保存/运行前校验图。
- **v7 主链路**：`query.input → question.resolve → (retrieval.dense ∥ retrieval.bm25 ∥ retrieval.hyde) → retrieval.rrf → retrieval.rerank → retrieval.corrective → evidence.gate → (answer.plan ∥ generation.llm) → citation.validate → answer.output`。
- **关键策略**：`workflow/nodes/evidence.py::gate()` 按 `score_kind` 分流——重排分/向量分用 `EVIDENCE_THRESHOLD`；若重排器不可用退化成词面分（`SCORE_LEXICAL`），默认 `RERANK_FAIL_CLOSED=true` 直接拒答，只有显式设 false 才用 `lexical.coverage` 保守放行。`workflow/nodes/generation.py::validate()` 逐条 claim 只保留能锚定到**本次检索块 id** 的结论，excerpt 需逐字核对父块，数字需被证据支持。
- **版本开关**：`service.selected_answer_version()` 决定 `medical-answer` 跑 v6 还是 v7，由 `DIALOGUE_STATE_ENABLED` 控制（默认 false）。显式请求 v7 但开关关闭时返回 409 `DIALOGUE_FEATURE_DISABLED`，**拒绝而不是降级**。启用需要先应用 `0009_conversation_dialogue_state.sql` 并显式发布 v7。

`src/medical_rag/retrieval.py` 的 `Retriever` 是遗留/兼容实现（一体化 dense+BM25+RRF+rerank），生产链路已改走 workflow 节点，修改检索行为时不要只改它。

### 三、入库是异步 outbox + 独立 worker

`POST /documents` 只做三件事：把文件落到 `DOCUMENT_UPLOADS_DIR`、在 SQLite 写 `document_jobs` 行、写 `task_outbox`。**API 进程不加载模型、不连 Milvus**（`jobs.py` 的 docstring 明确这一点），所以上传会立刻返回 202，前端必须轮询 `/document-jobs/{id}` 等入库成功后才能检索。

链路：`relay` worker 把 `task_outbox` 未发送行发布到 RabbitMQ（先确认 publish 再标 sent）→ `ingest` worker 消费 `medical_rag.document.ingest`，用 `IngestLock`（Windows `msvcrt`/POSIX `flock`）保证单写者，`DocumentProcessor.process()` 带租约续期跑 `document-ingestion.v1` 图：`document.parse → chunk → embed → store`，最后 `MilvusStore.replace()`。`memory` worker 消费 `medical_rag.memory.extract` 做长期记忆抽取。Broker 拓扑（exchange/queue/死信/quorum 参数/信封 schema）集中在 `messaging.py`，改消息格式要同时改 `decode_message()` 的白名单校验。

`ingest.chunk_document()` 做父子分块：子块按 `CHUNK_MAX_TOKENS` 嵌入，父块（`PARENT_MAX_TOKENS`）只作为生成上下文；`chunk_id = sha256(document_id:ordinal:parent_id:text)`，**改分块参数会改变块 ID，必须重新入库并重新映射评测题集**。

### 四、日志（Loguru）

`logging_setup.configure_logging(role)` 是唯一初始化点，只在真实入口调用：`api_server.main()`、`worker.main()`、`security_server.main()`。**不要在模块导入期配置日志**——测试会导入 `medical_rag.api`，否则会在仓库里落盘。文件 sink 在 `outputs/logs/<role>/<role>.log`，`LOG_ROTATION`/`LOG_RETENTION`/`LOG_COMPRESSION`/`LOG_JSON` 控制轮转、有效期、压缩与格式；`backtrace`/`diagnose` 固定关闭，凭证样式由 patcher 兜底遮蔽。第三方库走 `InterceptHandler` 汇入同一套 sink。

患者门户日志在 TOML 的可选 `[logging]` 段，**默认关闭**；开启后只允许固定短语与运维元事件，请求级数据仍只进 `security_audit_events`。`tests/conftest.py` 覆盖了同名 `caplog` fixture 把 Loguru 记录接管回来，所以脱敏断言依赖它，不要删除或放宽。

### 五、配置分层

`config.Settings`（pydantic-settings）读取 `env_file=<repo>/.env`，字段名大写即变量名，**进程环境优先于 `.env`**——这是测试能覆盖配置的基础，也是"终端里残留 `MILVUS_URI` 会连错服务"的成因。第三层是 `settings.json`（`SETTINGS_FILE`）：`settings_store.ModelSettings.apply_saved()` 覆盖模型 endpoint/Key，`.env` 保持只读。`.env.example` 是变量清单与默认值的权威注释来源，改动配置项要同步它。

### 六、前端两个独立构建

同一个 Vite 工程出两套产物：`vite.config.ts`（base `/ui/`，dev 把 `/api` 代理到 8001）产出管理端 `dist`；`vite.patient.config.ts`（base `/`，独立入口 `patient.html`）产出患者端 `dist-patient`。`src/patient/` 只被患者端引用，不要把它接进管理端路由，反之亦然。

## 仓库约定

- **测试不得产生真实模型费用**：普通用例用替身（monkeypatch `service.run_workflow`、伪造 `RagService`、`tests/document_helpers.upload_and_process`）。真实模型/Milvus/RabbitMQ 的验证只在显式开启的集成开关下运行。
- **不要声称全量套件通过**：`test_pinned_sources_remap_all_500_questions_with_default_chunking` 是已知未修的锚点映射失败，被 README 与 CI 口径显式排除。验证结论要写清范围与命令，`docs/security/progress.md` 是既有验证状态的唯一来源。
- **`unsafe-local-demo` 不是认证模式**：不得为它加反向代理、端口映射或转发服务，不得用它处理真实患者数据；也不得因新增患者入口而解除旧接口/worker 的锁定。
- **httpx 与 httpx2 并存是刻意的**：`httpx`（`>=0.27,<0.29`）供 OpenAI SDK、`web_search.py`、`patient_chat.py`；`httpx2` 供 Authlib 1.9 的 OIDC 与 Starlette TestClient。**不能把 httpx 的 transport 传给 Authlib**，否则 `httpx2` 内部断言失败。升级 Starlette/Authlib/OpenAI 主版本时须同时核对 `pyproject.toml` 的范围约束。
- **患者端不读 `.env`**，需要的新开关（如 `DIALOGUE_STATE_ENABLED`）必须在操作员进程环境注入；`start.ps1` 会显式转写这一个变量，取值只认 true/1/false/0。
- 患者侧所有请求先认证再解析 body，写操作要求 `__Host-` Cookie + `X-CSRF-Token` + 固定 Origin；新增患者路由必须走同一套 `authorized_transaction`，不能自己拼 SQL 或信任客户端传入的用户/机构/患者归属。
- 安全类改动沿用仓库既有节奏：`docs/security/*-brief.md`（需求）→ `*-report.md`（实施与验证记录）→ `*-review.diff`（冻结范围审查包），并在 `progress.md` 追加状态。不要把安全结论散落在代码注释里。
- 不提交 `.env`、`settings.json`、`data/*.pdf`、`outputs/`、`frontend/dist*`、虚拟环境与 `node_modules`。

## 文档地图

- `README.md` — 启动方式、安全边界、验证状态、HTTP 客户端约束。
- `docs/project-structure.md` — 完整目录说明与各模块职责。
- `docs/short-term-memory.md` — 短期记忆、问题重写、检索纠错的设计与预算。
- `docs/security/production-readiness.md` — 生产准入清单（未批准）。
- `docs/security/progress.md` — 分阶段进度、验证命令与审查结论。
- `deploy/patient-gateway/README.md` — 独立患者 nginx 网关的渲染与启动。
- `workflows/*.json` — 实际执行的管线定义，改动架构时先读对应版本图。
