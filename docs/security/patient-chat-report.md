# 患者入口 ChatGPT 式界面与公开资料问答改造报告

2026-11-05 · 虚构数据开发环境 · 非生产验收

## 范围

患者入口（OIDC 独立应用）从「登录后先看身份表单页」改为 ChatGPT 式问答产品：

- 登录成功后直接落在会话界面：左侧「＋ 新对话」、最近会话、分页与账户入口，中间消息区，底部输入框，左下角头像菜单显示本系统账户、机构、角色、核验状态并可退出登录。
- 首条问题自动创建会话，标题取问题前 30 字；后续问题追加到当前会话。
- 发送问题后，患者应用先做身份/授权/归属预检，再调用本机公开资料 RAG 答案服务，最后重新校验并保存 `user` / `assistant` 两条 turn 与引用。
- 未配置 `[chat]` 时退化为只保存文字记录，不伪造助手回复。

## 架构与安全边界

```text
浏览器 (patient.html) --HTTPS/Cookie/CSRF--> 患者应用 POST /chat
  1) SessionStore 校验会话 + CSRF
  2) 短事务：角色/绑定/患者归属/当前健康信息授权 + 会话归属（CHAT_ASK 审计）
  3) 调本机 RAG 答案服务（不在事务内、不持有患者行锁）
  4) 短事务：重新校验后才写入 user+assistant 两条 turn（TURN_APPEND 审计）
```

- 预检发生在问题离开进程之前：未授权、撤回中或引用他人会话时，问题不会到达模型。
- 持久化前二次校验：生成期间撤回授权、角色/绑定/会话失效时，丢弃已生成回答，不写库。
- 答案服务只允许回环 `http(s)` 来源，浏览器永远拿不到其地址；上游任何非 200、畸形响应或连接错误都映射为固定 `CHAT_UNAVAILABLE` 503，不回显上游正文、URL 或模型输出。
- 患者问题**不读取旧单用户 memory/profile**：`/answers` 新增 `memory` 开关，患者客户端固定发送 `memory: false`，避免旧演示画像或记忆进入患者回答；旧前端仍默认保留原行为。
- 问题 ≤16000 字符、答案 ≤20000、引用 ≤50 条，引用字段与长度逐项限制；服务端 CSRF 必需，请求体禁止额外字段。
- 管理角色不因本入口获得患者内容权限；未授权时 UI 显示闸门而不是空列表。

## 数据库与配置

- 新增 `database/migrations/0008_conversation_answers.sql`：
  - `conversation_turns.role`（`user`/`assistant`，默认 `user`）；
  - `conversation_turns.citations`（jsonb 数组，可空）；
  - `conversation_turns.ordinal`（identity，用于问答同一事务内的稳定排序，避免 `created_at` 并列）；
  - 审计 action 约束新增 `chat.ask`。
- `database/patient_runtime_grants.sql` 与 `security_server.preflight` 同步新增列；运行角色仍无 UPDATE/DELETE/TRUNCATE 权限。
- 新增可选 `[chat]` 配置：`base_url`（必须回环）、`timeout_seconds`（1–300）、`top_k`（1–20）；缺失时问答禁用、`/me.permissions.chat=false`。
- 演示配置 `outputs/patient-demo/config.toml` 指向本机 8001 的 `unsafe-local-demo` `/answers`，并把告知版本升到 `demo-notice-2026-12`，明确“提问会发送给本地检索与模型服务，不接入医院病历，不处理真实患者资料”。

## HTTP 客户端冲突的真实根因与修复

`httpx2` 与 `httpx` 并存时，Authlib ≥1.9 内部使用 `httpx2`；把项目原来的 `httpx`（v1）transport 交给它，会在 `httpx2` 的 `AsyncClient` 内触发 `assert isinstance(response.stream, AsyncByteStream)`。表现为真实登录回调 401（异常被 Authlib 边界吞掉）。

修复：

- `src/medical_rag/oidc_login.py` 整体改用 `import httpx2 as httpx`。
- 注入 mock transport 的 OIDC 测试（`tests/test_oidc_login.py`、`tests/test_provider_logout.py`、`tests/test_signing_key_http.py`）与 Authlib 同步客户端辅助（`tests/keycloak_operator.py`）同步改用 `httpx2`。
- `pyproject.toml` 显式钉住 `httpx>=0.27,<0.29`、`httpx2>=2.16,<3`、`openai>=1.60,<2`；README 写明分工与失败模式。

## 前端

- `model.ts` 增加 `permissions.chat`、`role`、`citations`、`chatResult` 严格解析；引用字段逐项校验。
- 发送走 `POST /chat`，等待期间显示用户问题与「正在检索公开资料并生成回答…」占位；成功后重载服务端会话与 turn 作为唯一事实。
- 助手消息左对齐显示，引用块 ID 显示为 `[1]` 并在气泡内列出“文件名 · 章节 · 页码”。
- 问答服务 503 显示内联错误并保留会话与草稿，可重试；401/403 仍走整体清空与锁定逻辑。
- 未配置问答时显示“当前未配置问答服务”，发送回退为文字记录接口。
- 保留既有安全行为：失焦/隐藏清空、generation 令牌丢弃旧响应、CSRF 仅内存、不写 localStorage、权限门控与 403。

## 改动文件

- 后端：`patient_chat.py`（新增）、`patient_service.py`、`patient_api.py`、`security_config.py`、`security_server.py`、`security_audit.py`、`oidc_login.py`。
- 数据库：`0008_conversation_answers.sql`（新增）、`patient_runtime_grants.sql`。
- 测试：`tests/test_patient_chat.py`（新增）、`tests/test_patient_vertical.py`、`tests/test_oidc_login.py`、`tests/test_provider_logout.py`、`tests/test_signing_key_http.py`、`tests/keycloak_operator.py`、`tests/test_security_server.py`、`tests/test_oidc_login.py` 迁移列表。
- 前端：`PatientApp.tsx`、`AccountMenu.tsx`、`model.ts`、`patient.css`、`patient-app.test.tsx`、`patient-support.test.tsx`。
- 演示：`outputs/patient-demo/config.toml`、`secrets/health-notice.txt`、`verify.py`、`verify_chat.py`（新增）、`apply_migration.py`（新增）、`browser_check.py`。

## 验证结果（2026-11-05）

- 后端非集成广范围：
  `python -m pytest -q -m "not integration" --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking`
  → **1622 passed, 5 skipped, 354 deselected, 1 warning**（`fitz` 既有 FutureWarning）。
- 患者实际 PostgreSQL：`tests/test_patient_vertical.py` → **57 passed**，含未授权不调用答案服务、撤回期间丢弃回答、跨患者会话 404、CSRF/边界、静态 503、引用清洗、问答角色与引用持久化。
- 认证/患者/支持实际 PostgreSQL 组合（OIDC、provider logout、signing key HTTP、patient chat、security server、support cases、MFA support）→ **284 passed**。
- 前端：`lint` exit0；`npm test` → **20 files / 215 tests passed**；`build` 与 `build:patient` exit0。
- 本地演示（真实 Keycloak 26.8.1 + PostgreSQL + TLS）：`verify.py` **14/14**；`verify_chat.py` 真实提问返回 200、`abstained=false`、1 条引用、turn 顺序为 `user`→`assistant`。
- 真实 Chrome：登录即会话页、头像菜单、助手气泡 1 个、引用行 1 条；截图 `outputs/patient-demo/screenshots/patient1-thread.png`、`patient1-chat.png`、`patient1-account.png`、`support1-gate.png`。

## 未完成与边界

- 未实现 token 级流式输出；当前是一次性回答 + 等待占位。SSE 可作为下一步，但必须同时处理断连、持久化与审计。
- 演示经由本机 `unsafe-local-demo` `/answers` 桥接，**不是生产架构**；生产需要内部认证的 RAG 服务、账号级限流、网络与模型审批。
- 授权用途仍是 `health_information`，告知文本已披露模型处理；生产建议单独的 `external_model` 用途/告知，而不是复用健康信息授权。
- 未做真实机构 IdP、医院模型接入、临床/法律验收或生产部署；旧知源单用户应用仍独立且默认锁定。
- `patient_chat.py` 仍使用 `httpx`（与 OpenAI 同栈）；OIDC 使用 `httpx2`。两栈并存是有意选择，见 README。

## 追加：连续追问的结构化会话状态（2026-11-07）

本次在患者入口新增「结构化会话状态」，仍默认关闭。

- 数据库：`0009_conversation_dialogue_state.sql` 增加 `conversations.dialogue_state/dialogue_revision/context_version`
  与 `conversation_turns.answer_type`；`patient_runtime_grants.sql` 只追加三列 `SELECT/UPDATE` 与
  `answer_type` 的 `SELECT/INSERT`；`security_server.preflight` 对 `conversations` 改成列扫描，
  其他列或表级 `UPDATE` 仍拒绝启动。
- 事务：`ask` 的预检改成需要 `PATIENT_WRITE` + CSRF 的短事务；带上下文的请求在预检事务内
  `read_snapshot`（`FOR SHARE`），模型调用期间不持锁；最终事务 `FOR UPDATE` 锁会话并按
  `context_version` CAS 提交，冲突返回 409 `CONVERSATION_CONTEXT_CHANGED`，不保存过期答案。
- 单独 `append_turn` 同样锁会话并递增 `context_version`，因此 ask+append 交错不会让旧答案落地。
- 患者调用 `/answers` 时由后端附带 `dialogue_context`（浏览器不可发送、不携带凭据），
  结果中的 `dialogue_resolution` 经严格校验后才保存；患者响应只增加 `answer_type`，不下发内部状态。
- 前端：澄清显示为普通补充问题（不显示「证据不足」），刷新后仍保留类型；409 固定提示
  「会话已更新，请重新发送问题。」并保留输入与会话，不自动重试。
- 开关：`DIALOGUE_STATE_ENABLED` 由操作员进程环境注入（患者配置 TOML 不新增字段），
  两个进程需同时启用；关闭时患者写入仍递增 `context_version`，重新启用能发现状态已过期。

边界不变：仍不是生产医疗合规，不接入医院病历，不把回环连接描述成机构级认证。
