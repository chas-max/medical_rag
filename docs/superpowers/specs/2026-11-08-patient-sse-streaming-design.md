# 患者门户 SSE 流式问答设计

日期：2026-11-08（Asia/Singapore）

状态：设计已获用户确认并于 2026-11-08 实施；实现与本文一致，验证记录见第 11 节。

范围：仅患者门户链路（RAG 答案服务 → 患者后端 → 患者网关 → 患者前端）。旧单用户 `/chat/runs/stream` 不在范围内，其行为不得改变。

## 1. 目标与已确认选择

患者提问后，答案中的**已通过校验的结论**应在生成过程中逐条出现，而不是等 10–40 秒后整段返回。

已与用户确认的两条产品决策：

1. **终态被否定时立即清空已流出的文字**，只显示固定提示；不保留"未通过校验"的内容在屏幕上。
2. 采用分层中继方案（见第 3 节），不做伪流式，不直连旧聊天端点。

非目标：

- 逐 token 展示模型原始输出（未经引用与数值校验的文本不得进入患者界面）。
- 停止/取消按钮、断点续传、消息重发、离线缓存。
- 旧单用户聊天的事件名、取消语义、前端 `streamRun` 改造。
- 患者记忆、病历、真实医院身份服务接入。

## 2. 当前实现与缺口依据

| 环节 | 现状 | 位置 |
|---|---|---|
| RAG 答案 | 只有非流式 `POST /answers` | `src/medical_rag/api.py:358` |
| RAG 服务 | `RagService.answer()` 无 `emit` 参数，无法向上接线 | `src/medical_rag/service.py:193` |
| 旧聊天 | 已端到端 SSE，含线程桥与 JSON 快照/深度校验 | `src/medical_rag/chat_api.py:69` |
| 生成器 | 已能逐条推送**通过校验**的结论与分析段 | `src/medical_rag/workflow/nodes/generation.py:72` |
| 患者客户端 | 阻塞 `httpx.Client.post("/answers")`，`Accept: application/json` | `src/medical_rag/patient_chat.py:198` |
| 患者接口 | `POST /chat` 同步返回，落库后才响应 | `src/medical_rag/patient_api.py:233` |
| 患者服务 | `ask()` 一次完成预授权 → 生成 → 再授权 → 落库 | `src/medical_rag/patient_service.py:335` |
| 患者前端 | `await request("/chat")` 拿到整段答案后重拉轮次 | `frontend/src/patient/PatientApp.tsx:485` |
| 独立网关 | location 白名单**不含 `/chat`**，患者问答经网关不通 | `src/medical_rag/patient_gateway.py:156` |

因此本改动必须跨 RAG 服务、患者后端、网关、患者前端四层，且不得破坏既有的"未落库不持久、撤权即丢弃"语义。

## 3. 备选方案与取舍

| 方案 | 做法 | 结论 |
|---|---|---|
| A 分层中继 | RAG 新增 `/answers/stream`；患者端新增 `/chat/stream` 逐帧裁剪；浏览器 fetch 解析 | 采用 |
| B 患者端直连旧 `/chat/runs/stream` | 复用既有聊天 SSE | 否决：会把患者问答写入本地 SQLite 历史与长期记忆/会话向量，破坏入口隔离；该端点还要求 `conversation_id` 与 `memory_consent` 语义 |
| C 伪流式 | 同步取完整答案后按句切片推送 | 否决：首字节延迟不变，不改善任何感知问题 |

## 4. 总体架构

```text
浏览器 (fetch + ReadableStream)
  │  POST /chat/stream   X-CSRF-Token, Cookie, Origin
  ▼
患者后端 (FastAPI, 直接 TLS)
  │  ① 预授权 + 会话归属 + 授权校验（返回响应对象之前完成）
  │  ② 事件白名单裁剪：status / claim 只留 text / done / error
  │  POST /answers/stream   Accept: text/event-stream
  ▼
RAG 答案服务 (旧单用户进程, 回环)
  │  RagService.answer(emit=...) → WorkflowEngine → generation.draft(on_claim=...)
  │  status / claim / analysis / done / error
  ▼
```

四层各自持有自己的契约，不存在"上游帧原样透传"。患者层只认识 4 个事件名，其余一律丢弃，未知帧不构成错误。

## 5. 详细设计

### 5.1 RAG：新增 `POST /answers/stream`

请求体复用 `AnswerRequest`（`question`、`provider`、`top_k`、`memory`、`dialogue_context`），响应为 `text/event-stream`。

事件集与旧聊天对齐但明确裁剪，白名单常量放在新模块：

| 事件 | data | 说明 |
|---|---|---|
| `status` | `{stage, evidence, web_search}` | 生成开始时由 `draft` 节点发出 |
| `claim` | `{text, citations}` | 逐条，已过锚点与数值校验 |
| `analysis` | `{...}` | 现有启发式段落 |
| `done` | 与 `/answers` 的 `data` 完全一致 | 权威结果 |
| `error` | `{code, message, status}` | 脱敏，固定文案 |

实现要点：

1. 新增 `src/medical_rag/sse.py`，把 `chat_api.py:69-129` 的桥接逻辑（线程执行 + `asyncio.Queue` + `loop.call_soon_threadsafe` + JSON 快照/深度校验 + 终态锁）抽为 `bridge(execute, allowed_events)`；`event_frame()` 一并迁入。该模块不得引入 `config`、`pymilvus`、`generation` 等重依赖。
2. `chat_api.stream_response()` 改为薄包装，传入现有 `_UPSTREAM_EVENTS`；对外行为与测试结果不变。
3. `RagService.answer()`、`run()` 增加 `emit` 参数并透传到 `run_workflow()`。
4. 响应头必须含 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no`。
5. 流开始前的失败仍抛 `ApiError`，由现有异常处理器转成 JSON 信封（`security_lockdown` 中间件对 `/answers/stream` 与 `/answers` 同等生效）。

### 5.2 患者：新增 `POST /chat/stream`

请求体与 `/chat` 相同（`question`、可选 `conversation_id`），额外要求 `X-CSRF-Token`。

患者侧事件集刻意小于上游：

| 事件 | data |
|---|---|
| `status` | `{stage}` |
| `claim` | `{text}` |
| `done` | 与 `/chat` 的 `data` 完全一致（`conversation`、`question`、`answer`、`abstained`、`answer_type`、可选 `abstain_reason`） |
| `error` | `{code, message}` |

**不转发 `analysis`**：现有非流式 `/chat` 本来就不把它交给患者（`patient_chat._answer()` 是显式白名单），转发会凭空开出一条未经患者侧校验的数据通道。

**不转发 claim 的 `citations`**：引用集合以 `done` 中的为准，避免同一份证据被两套校验逻辑各判一次。

### 5.3 授权与落库时序

`PatientService.ask()` 拆为可复用的三段，`ask()` 自身改为调用同样三段，保证两条路径不会出现两份 SQL：

1. `prepare_stream(token, *, csrf_token, conversation_id, question) -> StreamPlan`
   `_operation(CHAT_ASK, mutation=True)` 内完成 CSRF 校验、会话归属校验、当前授权校验，读取 dialogue 快照。**必须在返回 `StreamingResponse` 之前调用**，使未授权请求根本不进入流。`StreamPlan` 只携带 `target`（为 `None` 表示需要新建会话）、`snapshot`、`question`，不含身份或授权快照。
2. 上游调用：`ChatClient.stream(question, dialogue=snapshot)` 逐帧产出。
3. `finish_stream(token, *, csrf_token, question, conversation_id, answer) -> JsonObject`
   用现有 `patient_chat._answer()` 校验上游 `done`，再开 `_operation(TURN_APPEND, mutation=True)` 事务：版本栅栏、双行写入、`_commit_semantics`，与今天的 `ask()` 完全一致。

线程模型：上游读取与 `finish_stream()` 都在**工作线程**执行，异步生成器只消费队列（与 `sse.bridge` 同一套做法）。原因是 `ChatClient.stream()` 与 `_operation` 都是同步阻塞调用，直接放进异步生成器会阻塞事件循环。由此得到一个确定结果：浏览器断开只会停止消费队列，不会终止工作线程，也不会改变落库决定（不变量 6）。

时序：

```text
prepare_stream ──(失败: 403/409/503 JSON，无流)──▶ 结束
      │ 成功
      ▼
StreamingResponse 开始 ──▶ status 帧
      │
      ├─ claim 帧 × N（临时，客户端标记为未确认）
      │
      ▼ 上游 done
finish_stream（新事务，重新鉴权 + 版本栅栏）
      ├─ 成功 ──▶ done 帧（权威）
      └─ 失败 ──▶ error 帧（不落库，客户端清空临时结论）
```

### 5.4 错误分界

这是硬约束，不是风格问题：

- **流开始前**：`PatientDenied`、`ChatUnavailable`、`ConversationContextChanged`、请求体非法 → 现有 JSON 信封与 HTTP 状态码。原因是 `create_auth_app` 的 `@app.middleware("http")` 只能改写已返回响应的头，无法在流开始后更换状态码。
- **流开始后**：只能是 `error` 帧，且错误码固定、文案固定（`CHAT_UNAVAILABLE`、`CONVERSATION_CONTEXT_CHANGED`、`PATIENT_ACCESS_DENIED`）。

上游 `error` 帧只用于提前终止本地上游读取，**其文本绝不转发**；患者侧 `error` 由本地异常类型决定。

### 5.5 客户端推进器（`ChatClient.stream`）

新增 `stream()` 生成器方法：`httpx.Client.stream("POST", "/answers/stream")`，`Accept: text/event-stream`，`trust_env=False`，不跟随重定向（沿用现有约束）。

逐帧校验：

- 客户端接受 `status`/`claim`/`analysis`/`done`/`error` 五种事件；未知事件丢弃而不是报错（上游未来新增事件不应打断患者链路）。`analysis` 由路由层丢弃，不进入患者事件集。
- `data` 必须是 JSON 对象；单帧超过 16384 字节视为协议错误。
- `claim` 的 `text` 必须是非空字符串且不超过 2000 字，否则丢弃该帧。
- 上游 HTTP 非 200、`Content-Type` 非 `text/event-stream`、连接中断 → `ChatUnavailable`。
- 超时沿用 `timeout_seconds`（`read` 逐块计时，默认 120 秒，覆盖"检索 + 首个结论"的静默期）。

### 5.6 患者前端

新增 `frontend/src/patient/stream.ts`（约 70 行）：`fetch` + `response.body.getReader()` + SSE 分帧。**不使用 `EventSource`**：它无法携带 `X-CSRF-Token`，也不能 POST 请求体。

`PatientApp.tsx`：

- `me().permissions.chat_stream` 决定走流式还是现有的同步 `/chat`；不做运行时探测。
- 流式路径维护 `draft: string[]` 临时结论；`claim` 追加，`status` 更新阶段提示。
- `done` → 清空 `draft`，用复用 `model.ts::chatResult()` 的同一份解析，然后按现有逻辑重拉会话列表与轮次。
- `error` → **清空 `draft`**，设置固定提示：撤回类错误显示"已撤回授权，本次回答未保存"，其余显示"问答服务暂不可用，请稍后重试"。
- 现有的 `pending.current` 并发闸门与 `live()` 生命周期检查保留，不新增第二个并发开关。

### 5.7 网关

`patient_gateway.py:156` 的 location 正则加入 `chat(/stream)?`。其余参数已满足流式要求，不改动：

- `proxy_buffering off`、`proxy_cache off`、`proxy_max_temp_file_size 0` 已存在；
- `proxy_read_timeout 180s` 覆盖生成期；
- `proxy_ignore_headers X-Accel-Buffering` 保留；
- 请求头白名单已含 `Cookie/Origin/Content-Type/Accept/Accept-Language/X-CSRF-Token`，流式不需要新头。

### 5.8 配置

`[chat]` 段新增 `streaming`（布尔，**默认 false**），`security_config._keys()` 白名单同步为 `base_url timeout_seconds top_k streaming`；`type(...) is bool` 严格校验，其他类型启动失败。

- `streaming=false`：`/chat/stream` **不注册**，`me()` 的 `permissions.chat_stream` 为 false。
- `streaming=true`：注册路由，`permissions.chat_stream` 为 true。

由此"半启用"在结构上不可能发生，前端也不需要探测端点是否存在。`me()` 的 permissions 是加字段，向后兼容。

## 6. 不变量

1. 任何到达患者界面的 `claim` 都来自现有 `generation.draft()` 的 `on_claim`，即已通过锚点与数值校验的结论；不引入新的未校验文本通道。
2. `done` 只在 `finish_stream()` 提交成功之后发送；客户端只以 `done` 为权威来源。
3. 终态被否定时，已流出的临时文字必须被清空，不留在屏幕上。
4. 上游 URL、模型输出、异常文本、未知字段不得进入患者响应；患者 `error` 文案本地固定。
5. 流式与非流式落库结果必须一致：同一 `_answer()` 校验、同一 `_operation` 事务、同一版本栅栏。同一问题在两种模式下产生的 `conversation_turns` 行结构必须相同。
6. 浏览器断开不改变落库决定：上游已给出完整答案且重新授权通过，则正常落库，只是不再发送帧。这与当前同步语义一致，代价是放弃一次生成，换取"问过就有记录"的可预期性。
7. 旧 `/chat/runs/stream`、`/answers`、工作流定义均不得被本改动修改行为。

## 7. 测试计划

新增：

- `tests/test_stream_sse.py`：`/answers/stream` 的帧序列与终态；`done` 与 `/answers` 的 `data` 等价；locked 模式下 503；事件白名单拒绝未知事件名与超深嵌套。
- `tests/test_patient_stream.py`：授权失败发生在流之前（JSON 403）；中途撤权导致 `error` 且无 `conversation_turns` 新增；`done` 前必有提交（可用事务顺序断言）；断连后仍落库；上游 `error` 文本不出现在响应中；未知事件被丢弃不掉流。
- `frontend/src/test/patient-stream.test.tsx`：跨 chunk 边界分帧、多行 `data:`、CRLF、`error` 后清空 `draft`、`done` 后走同一 `chatResult` 解析。

扩展：

- `tests/test_patient_chat.py`：`ChatClient.stream()` 的白名单、超大帧、`Content-Type` 不符、连接中断。
- `tests/test_patient_gateway.py`：渲染结果包含 `/chat` 与 `/chat/stream`，且仍不包含 `/ui`、`/documents`。
- `tests/test_patient_vertical.py`：`permissions.chat_stream` 随配置变化。

回归口径：`python -m pytest -q -m "not integration"`（排除已记录的 500 题锚点用例）。普通用例必须使用替身，不产生真实模型调用。

## 8. 灰度与回滚

1. `streaming=false` 为默认值，合入后患者端行为与今天完全一致。
2. 在演示环境打开 `[chat] streaming = true`，验证真实链路（含网关）后再改默认值。
3. 回滚只需把 `streaming` 改回 false 并重启患者服务；没有数据库迁移，没有需要清理的中间状态。

## 9. 风险

| 风险 | 说明 | 处置 |
|---|---|---|
| `BaseHTTPMiddleware` 缓冲 | `create_auth_app` 用 `@app.middleware("http")` 包裹所有响应；早期 Starlette 版本存在缓冲 SSE 的历史问题 | 实施时先用 `curl -N` 确认首字节即时到达；若被缓冲，把头部逻辑移出中间件而不是关闭安全头 |
| 静默期读超时 | 检索 + 首个结论可能长时间无帧 | 生成节点已在开始时发 `status`；`read` 超时取 `timeout_seconds`（默认 120 秒） |
| 网关漏配 | 白名单是手工维护的正则 | 网关测试断言 `/chat/stream` 可路由；`docker-compose.patient-gateway.yml` 路径不变 |
| 双重校验分叉 | 流式与非流式各自实现落库会漂移 | `ask()` 改为复用 `prepare_stream` + `finish_stream`，从结构上消除重复 |

## 10. 验证记录

2026-11-08：

- 真实 uvicorn + 真实 ASGI 中间件（`security_lockdown`）下测量 `/answers/stream`：响应头与首帧在 0.16 秒到达，1.5 秒后才产生第二帧与 `done`。**中间件不缓冲 SSE**，第 9 节的首个风险已排除。
- `python -m ruff check src tests` 通过。
- `python -m pytest -q -m "not integration"`（按仓库口径 deselect 已记录的 500 题锚点用例）：1843 passed，5 skipped，0 failed。
- `npm --prefix frontend test` 231 passed（22 个文件）；`run lint`、`run build`、`run build:patient` 均通过。
- 未验证：PostgreSQL 门控的三条患者集成用例（本机无 Docker）与真实 Keycloak/浏览器全链路。

## 11. 影响文件

- 新增：`src/medical_rag/sse.py`、`frontend/src/patient/stream.ts`
- 修改：`src/medical_rag/api.py`、`service.py`、`chat_api.py`、`patient_chat.py`、`patient_service.py`、`patient_api.py`、`patient_gateway.py`、`security_config.py`、`security_server.py`、`frontend/src/patient/PatientApp.tsx`、`deploy/patient-gateway/README.md`
- 未改动：`workflows/*.json`、`database/migrations/*`、旧聊天前端 `frontend/src/api/*`
