# 项目目录结构

本文件描述当前完整项目结构。安全改造已合并到 `master`
（`D:\medical-rag-resume`）；`codex/medical-security` 工作树保留为历史分支，
其本地 `outputs/` 目录保存任务验收证据。代码与文档以主目录为准。

测试运行产生的 `pytest-tmp-*` / `.pytest-tmp-*`、`.pytest_cache`、
`.ruff_cache`、`__pycache__`、`.coverage`、前端 coverage 与测试报告均属
可再生成产物，已清理并写入 `.gitignore`。`outputs/` 保留为运行日志/验收
证据目录，不是源码。

## 顶层

```text
medical-rag-resume/
├─ README.md                    项目说明、启动与安全状态
├─ pyproject.toml               Python 包、依赖、pytest/ruff 配置
├─ .env.example                 环境变量示例（不提交实际 .env）
├─ .streamlit/config.toml       Streamlit 控制台本地主题（DeepSeek 风格浅色）
├─ start.cmd, start.ps1         一键启动本机演示（RAG + 患者门户）
├─ start-ui.cmd, start-ui.ps1   Streamlit 问答与指标控制台启动器（可选；.cmd 为 cmd/双击入口）
├─ docker-compose.yml           本地 Milvus/RabbitMQ 等依赖
├─ docker-compose.gateway.yml   旧明文 HTTP 网关实验（未接入患者入口）
├─ docker-compose.patient-gateway.yml  独立患者 HTTPS 网关（仅网关容器）
├─ docker-compose.streamlit.yml Streamlit 控制台容器（仅回环 8501；容器内问答不可用）
├─ src/medical_rag/             后端 Python 包
├─ tests/                       后端单元/集成/安全测试
├─ frontend/                    React 前端（旧聊天界面 + 患者独立入口）
├─ workflows/                   可编排工作流 JSON 定义
├─ database/                    患者隔离 PostgreSQL 迁移与授权脚本
├─ deploy/                      网关、身份策略、患者配置与 Streamlit 容器示例
├─ docs/                        安全需求、报告、进度与复审记录
├─ data/                        公开语料、评测集与本地运行数据
├─ tools/ragas/                 可选 Ragas 独立环境依赖与说明
└─ outputs/                     运行日志与验收证据（gitignore）
```

## 后端 `src/medical_rag/`

```text
src/medical_rag/
├─ 应用入口与聊天
│  ├─ api.py, api_server.py, chat_api.py, service.py, chat_runtime.py, chat_history.py
│  ├─ short_term.py, summarize.py, conversation_index.py
│  ├─ dialogue_models.py, dialogue_state.py, dialogue_flags.py, dialogue_evaluation.py
│  └─ streaming.py, types.py
├─ 检索与 RAG
│  ├─ retrieval.py, generation.py, embeddings.py, lexical.py
│  ├─ store.py, tokenization.py, web_search.py, tools.py
│  └─ numeric_validation.py, model_json.py, selfcheck.py
├─ 文档入库与任务
│  ├─ ingest.py, jobs.py, worker.py, messaging.py
├─ 记忆与画像
│  ├─ memory.py, memory_api.py, memory_context.py, memory_worker.py
│  └─ profile_intent.py, profile_models.py, profile_policy.py, profile_store.py
├─ 评测
│  ├─ evaluate.py, evaluation*.py
│  ├─ ragas_adapter.py          报告/题库 → Ragas 样本、指标适用性、报告合并
│  ├─ ragas_llm.py              Qwen 结构化输出与 BGE embeddings 适配
│  ├─ ragas_prompts.py          中文对齐 prompt（可选，默认开启）
│  ├─ ragas_evaluation.py       Ragas 指标构建、并发评分、汇总
│  └─ ragas_evaluate.py         对已保存报告做 post-hoc Ragas 评分 CLI
├─ 患者身份/安全
│  ├─ security_config.py, security_server.py, security_audit.py, public_errors.py
│  ├─ oidc_login.py, oidc_identity.py, identity_sessions.py, signing_keys.py
│  ├─ provider_tls.py, provider_logout.py
│  ├─ access_policy.py, consent_policy.py
│  ├─ patient_api.py, patient_service.py, patient_repository.py
│  ├─ patient_chat.py, patient_dialogue.py
│  ├─ patient_gateway.py
│  └─ support_service.py
├─ 配置与设置
│  └─ config.py, settings_store.py, logging_setup.py
├─ 展示界面（可选）
│  └─ ui/                      Streamlit 租户问答与指标控制台
│     ├─ __init__.py
│     ├─ __main__.py            `python -m medical_rag.ui` 入口
│     ├─ app.py                登录门、侧边栏、页面路由与租户会话校验
│     ├─ chat_page.py          问答页：SSE 流式、引用资料、运行轨迹、会话删除
│     ├─ stats_page.py         数据统计页：多版本指标、延迟/吞吐、任务与入库
│     ├─ api_client.py         后端 HTTP/SSE 客户端（回环校验、错误码映射）
│     ├─ auth.py               租户账号、scrypt 口令哈希、登录锁定
│     ├─ demo.py               演示账号自动创建与登录页凭据自动填入
│     ├─ tenants.py            租户账号管理 CLI
│     ├─ stats.py              问答指标存储与聚合、评测报告多版本对比
│     ├─ state.py              视图状态与租户会话映射
│     ├─ storage.py            界面状态库 SQLite 基类
│     ├─ data.py               评测报告 JSON 扫描与只读数据辅助
│     └─ launcher.py           启动器（仅回环、参数解析、拉起 Streamlit）
└─ workflow/                    工作流引擎与节点
   ├─ engine.py, definition.py, contracts.py, validator.py
   ├─ repository.py, registry.py, run_schema.py, bootstrap.py
   ├─ agentic*.py
   └─ nodes/                    input/question/planning/retrieval/evidence/
                                generation/merge/agent/documents/output
```

旧单用户 API、聊天、记忆、模型设置、文档和后台 worker 默认锁定；患者入口
与旧入口隔离，不因新增安全模块而解除锁定。

## 测试 `tests/`

```text
tests/
├─ test_*.py                    单元测试与安全/患者/网关测试（数量以目录实际文件为准）
├─ conftest.py                  共享 fixture
├─ document_helpers.py          文档构造辅助
├─ keycloak_*.py                实际 Keycloak 运行时/表单/MFA/运维辅助
├─ patient_gateway_runtime.py   患者网关实际 socket + 临时 PostgreSQL 辅助
└─ fixtures/                    合成 TLS 上游等测试夹具
```

测试按标记区分：普通测试默认运行；`integration` 需要 Docker/实际基础设施，
未显式选择时跳过，选中后缺少基础设施应失败而不是静默改为 mock。

## 前端 `frontend/`

```text
frontend/
├─ src/
│  ├─ api/                      后端 API 客户端
│  ├─ chat/                     聊天界面组件与上传
│  ├─ components/               通用 UI/RunTrace/答案渲染
│  ├─ pages/                    聊天页面
│  ├─ patient/                  独立患者入口
│  ├─ settings/                 模型/外观/记忆设置
│  ├─ workflow/                 运行追踪
│  └─ test/                     Vitest/Testing Library 测试
├─ vite.config.ts               旧聊天前端构建
├─ vite.patient.config.ts       独立患者前端构建
├─ patient.html                 患者入口 HTML
├─ dist/                        旧前端构建产物（gitignore）
└─ dist-patient/                患者前端构建产物（gitignore）
```

## 数据库、部署与文档

```text
database/
├─ migrations/0001…0008_*.sql   患者隔离/身份/MFA/撤权/支持/问答迁移
└─ patient_runtime_grants.sql   非所有者运行角色的最小授权配方

deploy/
├─ gateway/                     旧 HTTPS/明文后端实验配置
├─ identity/                    Keycloak MFA 策略片段与接入说明
├─ patient/                     患者应用 TOML/密钥配置示例
├─ patient-gateway/             独立患者 nginx 网关配置、启动脚本、运维 README
└─ streamlit/                   Streamlit 控制台容器（Dockerfile；容器内问答不可用）

docs/
├─ project-structure.md         本文件
├─ streamlit-ui.md              Streamlit 租户问答与指标控制台：架构、租户/CLI、启动与排障
└─ security/
   ├─ progress.md               任务进度与验收记录
   ├─ production-readiness.md   生产准入清单（未批准）
   ├─ *-brief.md                各任务需求
   ├─ *-report.md               实施与独立复审报告
   └─ *-review.diff             冻结范围的审查包
```

## 运行与清理约定

```powershell
# 普通测试（不产生真实模型费用）
python -m pytest -q -m "not integration"

# 前端
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
npm --prefix frontend run build:patient

# Streamlit 问答与指标控制台（可选）：先启动 RAG 后端，再装可选依赖并启动
.\start.ps1                          # 终端 1：本机 RAG 后端（127.0.0.1:8001，unsafe-local-demo），否则问答不可用
uv pip install --python .venv\Scripts\python.exe -e ".[ui]"
.\start-ui.cmd                       # 终端 2：cmd/双击入口；登录页默认自动填入演示账号密码，点「登录」即可
.\.venv\Scripts\python.exe -m medical_rag.ui.tenants add alice --generate   # 可选：关闭自动登录时手工建号
.\.venv\Scripts\python.exe -m pytest tests/test_ui_auth.py tests/test_ui_demo.py tests/test_ui_state.py tests/test_ui_stats.py tests/test_ui_api_client.py tests/test_ui_data.py tests/test_ui_launcher.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_ui_app.py -q   # 需已装 streamlit，未装时跳过
.\start-ui.cmd -Status               # 运行状态、健康检查与 RAG 后端可达性
.\start-ui.cmd -Stop                 # 只停进程，保留 data/streamlit/ui-state.sqlite3（删除该文件才重置账号/指标/视图）
```

测试临时目录、缓存、覆盖率与前端测试报告已加入 `.gitignore`，可随时删除。
`outputs/`、`data/*.pdf`、`data/chat-history.sqlite3*`、`data/streamlit/`、
`frontend/dist*`、`.env`、虚拟环境和 `node_modules` 均有独立忽略规则；不要提交实际
密钥、真实患者数据或生产证书。
