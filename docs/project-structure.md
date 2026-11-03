# 项目目录结构

本文件描述 `codex/medical-security` 工作树的完整结构（当前安全改造分支）。
`D:\medical-rag-resume` 是未合并的安全改造前基线，缺少 `database/`、`deploy/`、
`docs/` 以及患者身份/网关模块和独立患者前端；需要完整功能时以本工作树为准。

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
├─ docker-compose.yml           本地 Milvus/RabbitMQ 等依赖
├─ docker-compose.gateway.yml   旧明文 HTTP 网关实验（未接入患者入口）
├─ docker-compose.patient-gateway.yml  独立患者 HTTPS 网关（仅网关容器）
├─ src/medical_rag/             后端 Python 包
├─ tests/                       后端单元/集成/安全测试
├─ frontend/                    React 前端（旧聊天界面 + 患者独立入口）
├─ workflows/                   可编排工作流 JSON 定义
├─ database/                    患者隔离 PostgreSQL 迁移与授权脚本
├─ deploy/                      网关、身份策略、患者配置示例
├─ docs/                        安全需求、报告、进度与复审记录
├─ data/                        公开语料、评测集与本地运行数据
└─ outputs/                     运行日志与验收证据（gitignore）
```

## 后端 `src/medical_rag/`

```text
src/medical_rag/
├─ 应用入口与聊天
│  ├─ api.py, chat_api.py, service.py, chat_runtime.py, chat_history.py
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
│  └─ evaluate.py, evaluation*.py
├─ 患者身份/安全
│  ├─ security_config.py, security_server.py, security_audit.py, public_errors.py
│  ├─ oidc_login.py, oidc_identity.py, identity_sessions.py, signing_keys.py
│  ├─ provider_tls.py, provider_logout.py
│  ├─ access_policy.py, consent_policy.py
│  ├─ patient_api.py, patient_service.py, patient_repository.py
│  ├─ patient_gateway.py
│  └─ support_service.py
├─ 配置与设置
│  └─ config.py, settings_store.py
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
├─ test_*.py                    单元测试与安全/患者/网关测试（99 个测试文件）
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
├─ migrations/0001…0007_*.sql   患者隔离/身份/MFA/撤权/支持请求迁移
└─ patient_runtime_grants.sql   非所有者运行角色的最小授权配方

deploy/
├─ gateway/                     旧 HTTPS/明文后端实验配置
├─ identity/                    Keycloak MFA 策略片段与接入说明
├─ patient/                     患者应用 TOML/密钥配置示例
└─ patient-gateway/             独立患者 nginx 网关配置、启动脚本、运维 README

docs/security/
├─ progress.md                  任务进度与验收记录
├─ production-readiness.md      生产准入清单（未批准）
├─ *-brief.md                   各任务需求
├─ *-report.md                  实施与独立复审报告
└─ *-review.diff                冻结范围的审查包
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
```

测试临时目录、缓存、覆盖率与前端测试报告已加入 `.gitignore`，可随时删除。
`outputs/`、`data/*.pdf`、`data/chat-history.sqlite3*`、`frontend/dist*`、
`.env`、虚拟环境和 `node_modules` 均有独立忽略规则；不要提交实际密钥、
真实患者数据或生产证书。
