# 短期会话记忆与问题重写

本文件记录「近 k 轮 + 历史摘要 + 规则触发重写 + 检索反馈重试」这一层的设计、
已落地范围与后续步骤。长期记忆（画像 / 情景）仍由 `memory.py` 负责，两者是
**两条独立通道**，不合并。

## 1. 目标与边界

- 生成时始终带上**本会话最近 3 轮原文 + 更早轮次的滚动摘要**，让追问能被正确理解。
- 问题重写**只在需要时发生**：由纯规则判定，不额外增加一次「为了判断而调用模型」。
- 检索不达标时**最多再重写并检索一轮**，且结果不得比第一轮更差。
- 会话内容一律视为**不可信上下文**：可用于消解指代，**不是医学证据**，不得进入引用校验。
- 短期上下文与纠错都是可降级的增强：失败只会让答案退回第一轮，不会让问答报错。

## 2. 第一步：短期上下文（已完成）

### 2.1 数据来源

| 数据 | 位置 | 说明 |
|---|---|---|
| 已完成的问答轮次 | `turns`（`chat_history.py`） | 权威来源；`recent_turns()` 只取已写入答案的轮次 |
| 滚动摘要 | `conversation_summaries` | 覆盖到 `through_turn` 的派生数据，可随时重建 |

预算（`short_term.py`）：单轮问题 ≤200 字、答案 ≤400 字，窗口总量 ≤1200 字，摘要 ≤800 字。

### 2.2 装载与注入

`ChatRuntime.run()` 在跑工作流前装载 `ShortTerm`，通过 `ExecutionContext.short_term`
（与 `memory_context` 平行的不可变快照）一路传到节点与生成器：

- system 追加 `SHORT_TERM_BOUNDARY`（不可信、非证据、当前问题优先）；
- user JSON 增加独立的 `short_term: {recent: [...], summary: "..."}`；
- `evidence` 列表不受影响，引用校验仍只看到检索块。

### 2.3 规则1：前置路由（`question.pre_route`）

纯规则、零模型调用，只认显式线索：

| 线索 | 路由 |
|---|---|
| 无会话上下文（默认行为） | `direct`（`require_context=true`，第一轮没有可消解的对象） |
| 承接词开头：那/这个/它/还有/然后/再… | `rewrite` |
| 跨轮指代短语：刚才/前面/你说的/上述/该药/这个病… | `rewrite` |
| 省略式追问：呢/然后呢/怎么吃… | `rewrite` |
| 药名误伤保护：那曲/那可/那他 | 不当作承接词 |
| 长度 >200 字 | `direct`（本身已完整） |

### 2.4 重写（`question.rewrite`）

- `route=direct`：**不调用模型**，原样返回问题；
- `route=rewrite` 且有上下文：输入 `{question, recent, summary}`，提示词只允许「消解指代 + 补全省略」，
  不新增医学事实、不回答问题；
- 输出仍受 ≤256 字约束，非法输出回退原问题；
- 未接线 `route` 的旧图（如 `medical-answer-expanded`）行为不变。

### 2.5 滚动摘要（`summarize.py`）

- 每轮回答落库后 `maybe_schedule()` 起一个后台线程；
- 只吸收「已滑出窗口」的轮次，且累计 ≥2 轮才调用一次模型；
- 第 5 节把边界改为按本会话真实有序记录计算（`digest_candidates`），
  不再用全局自增 ID 差推算；
- 已覆盖位置写入 `through_turn`，重复调用不会重复摘要；
- 8 秒超时、不重试、失败静默；无 API Key 时构造为禁用状态。

## 3. 第二步：检索反馈（已完成）

### 3.1 规则2 的位置

判定必须在 `rerank` **之后**、`evidence.gate` **之前**：gate 会 `Halt` 并剪掉整棵下游子树，
放在它后面的重试永远触发不了。

### 3.2 判定（`corrective.py::_needs_correction`）

```python
降级（reranker_unavailable）        → 不重试（重试修不好重排器，gate 本就 fail-closed）
len(evidence) < min_evidence(2)    → 重试
没有任何 kind ∈ {rerank, dense} 且 score ≥ threshold → 重试
```

只有门控真正信任的两种分数参与比较；lexical / bm25 / unknown 量纲不同，不能拿来比阈值。

### 3.3 一轮纠错做什么（`retrieval.corrective`）

```
重写（带窗口 + 摘要，route 强制 rewrite）
  → HyDE（对新 query）
  → dense + bm25
  → RRF 融合
  → 合并第一轮证据后重排（用新 query）
  → 取 top_k 返回；任何一步失败都退回第一轮证据
```

- `max_rounds` 上限 1，报告里记 `stop_reason / rounds / queries / before / after`；
- 重写结果与原问题相同 → `unchanged_query`，不做第二轮检索；
- 第二轮重排后为空 → `retry_empty`，保留第一轮证据（永不返回更差结果）；
- 重排降级 → `degraded`，不消耗模型调用。

### 3.4 工作流 `medical-answer` v6

> 第 5 节新增 v7（结构化会话理解）。`DIALOGUE_STATE_ENABLED=false` 时应用显式选择 v6，
> 本节描述的接线保持不变；v7 已发布，但默认开关仍为关闭。

```
query.input → question.pre_route → question.rewrite → dense / bm25 / retrieval.hyde
                                          ↓
                                rrf → rerank → retrieval.corrective → evidence.gate
                                          ↓                         ↓ history
                       answer.plan ──→ generation.llm ← 原问题 ────────┘
                                          ↓
                               citation.validate → answer.output
```

v3 / v4 / v5 保留归档，`get_published` 取最高版本。

## 4. 第三步：历史对话向量召回（已完成）

### 4.1 范围

- **默认只召回当前 `conversation_id` 的轮次**：集合里存 `conversation_id`，检索时用过滤表达式，
  跨会话读不到；需要跨会话时显式设置 `CHAT_RECALL_SCOPE=global`（见 4.6）。
- **只有规则2 触发时才召回**：规则1 的重写只用窗口 + 摘要，不碰向量库。

### 4.2 索引（`conversation_index.py`）

| 项 | 值 |
|---|---|
| 集合 | `medical_rag_chat_turns`（`chat_turn_collection` 可配） |
| 字段 | `id` / `conversation_id` / `turn_id` / `text` / `created_at` / `dense(512)` |
| 索引 | HNSW + COSINE（M=16, efConstruction=200） |
| 主键 | `sha256(conversation_id:turn_id)[:32]`，重复写入覆盖同一行 |
| 文本 | `问：…\n答：…`，截断 2000 字 |
| 时间戳 | `created_at`（秒），跨会话召回时用于时间衰减 |
| 写入 | 回答落库后由后台线程 `upsert`，单进程最多一个在飞 |
| 召回 | 相似度 < 0.35 的邻居直接丢弃（弱邻居会把重写带偏） |

### 4.3 注入

`corrective` 在规则2 命中后召回（种子 = 上一轮用户原话 + 当前问题），产出独立的
`history` 输出：

```
corrective.history → question.rewrite.history   （并入重写 payload）
corrective.history → generation.llm.history     （并入生成 payload）
```

`history` 与 `short_term`、`evidence` 三者互不复用：历史片段**不是**可引用证据，
引用校验与门控仍只看检索块。生成提示词追加 `history_boundary`。

### 4.4 失败语义

- 集合不可用 / schema 不匹配 → 服务启动时关闭召回，主链路不受影响；
- 写入或召回异常 → 静默降级，本次无历史，重写与作答照常；
- `conversation_id` 不在 `[A-Za-z0-9_-]{1,128}` 内 → 直接跳过（防过滤表达式注入）。

### 4.5 删除会话的级联

`DELETE /conversations/{id}` 一次删干净，顺序是「先权威、后派生」：

1. `conversation_summaries`、`turns`、`chat_requests`、`conversations`、`runs`（SQLite 事务内）；
2. `memory_jobs`（未处理的原始问答）、`profile_confirmations`、由这些 run 派生的 `memory_episodes` 与文件；
3. Milvus 会话集合里该 `conversation_id` 的全部向量；
4. 进程内 `RagService.runs` 缓存。

有请求正在处理时删除会被拒绝（409 `REQUEST_IN_PROGRESS`），避免把正在写入的答案删掉一半。
画像条目（`memory_profiles`）**不删**：那是用户明确陈述的长期信息，有自己的生命周期。

### 4.6 跨会话召回开关

`CHAT_RECALL_SCOPE=global` 时不再带 `conversation_id` 过滤，改为按 `created_at` 做半衰期衰减
（`CHAT_RECALL_HALF_LIFE_DAYS`，默认 7 天）：`score * 0.5 ** (age_days / half_life)`。
没有时间戳的历史行按「半个半衰期」处理，既不会被永久置顶，也不会直接消失。
默认仍是 `conversation`——跨会话串话的风险大于收益，需要时再显式打开。

## 5. 已知取舍

| 取舍 | 原因 |
|---|---|
| 第一轮路由不重写 | 没有可消解的对象，改写只会带来检索漂移 |
| 规则2 不因重排降级触发 | 降级是模型不可用，重试同一路径无意义；gate 已 fail-closed |
| 重排第二轮的合并池 | 用新 query 统一重排，避免新旧分数混排；池子被 rerank_k 截断以控延迟 |
| 会话不进 evidence | 引用校验只认真实检索块，混入即整条结论被丢弃 |
| 后台摘要不重试 | 单次失败只让摘要在下一次触发时补齐，不值得为一个可选增强拖慢主链路 |
| 纠错失败静默降级 | 它是第二次尝试，第一轮结果本身是可交付的 |
| 历史召回只在本会话内 | 跨会话串话的收益远小于风险；需要时用 `CHAT_RECALL_SCOPE=global` 显式打开 |
| 弱相似度邻居直接丢弃 | 一个不相关的历史片段会把重写和第二轮检索一起带偏 |
| 删除先权威后派生 | 中途失败只会留下孤儿读模型，不会留下用户已删内容的副本 |

## 5. 第四步：结构化会话状态（v7，默认关闭）

本节记录 2026-11-07 设计（`docs/superpowers/specs/2026-11-07-dialogue-state-design.md`）的落地。
开关 `DIALOGUE_STATE_ENABLED` 默认 `false`；关闭时应用仍显式选择 v6，行为与上一节完全一致。

### 5.1 严格契约（`dialogue_models.py`）

- 所有模型 `extra="forbid"`、`strict=True`、`frozen=True`，容器用 tuple，快照不可被下游修改。
- `ConversationState` 记录 `active_topic`、`active_intent`、`active_intent_text`、
  `last_resolved_question`、`pending_clarification` 与 `revision`/`through_turn_id`；
  序列化后 >8 KiB 直接拒绝，不截断成非法 JSON。
- `DialogueSnapshot` = 状态 + 最近 3 轮原文 + 摘要 + `context_version`；
  窗口预算与旧实现共用 `short_term.recent_window()`，两条路径不会给出不同窗口。
- `DialogueRun` 是请求级结果槽：解析结果只放在调用者持有的对象里，
  不进 `WorkflowRunResult`、trace、SSE 或公开 output。

### 5.2 单次解析（`question_resolver.py`）

- 每轮最多一次 `complete_with_timeout(provider, system, user, 8)`，不重试、不退回无超时 `complete`。
- 先做唯一确定性快路径：无状态、无历史、无摘要且以代词开头 → 直接请求补充对象，不调用模型。
- 模型输出经 `ResolverReply` 严格校验 + `validate_resolution_sources`：
  来源 must 是 `current` / recent 的 turn、角色匹配、实体确实出现在对应文本或已持久化状态里，
  数字约束不得新增。任何失败都退回固定澄清模板。
- `clarification_answer()` 只拼接已校验候选名；无候选时固定「请补充具体讨论对象，并把想了解的问题说完整。」，
  返回 `citations: []`、`abstained: false`、`answer_type: "clarification"`。

### 5.3 状态归约（`dialogue_state.py`）

- `advance_state()` 是纯函数：`revision + 1`、`through_turn_id` 取本轮权威 turn，
  只把来源 `current` 改写成真实 turn ID。
- 澄清轮：有候选则替换待澄清项；解析故障（无候选）只维护提交边界，保留旧待澄清项，
  不清空仍有效的主题。
- `can_commit_semantics()` 用白名单决定是否推进主题：真实回答、澄清、
  `ABSTAIN_NO_EVIDENCE/LOW_SCORE/UNANCHORED` 可以；解析失败、截断、降级、数值不一致、生成失败不可以。

### 5.4 本地存储（SQLite）

- `conversation_states`：`conversation_id` 主键 + `state_json` + `revision` + `context_version` + `through_turn_id`。
- `ChatHistoryStore.dialogue_snapshot()` 在**一个读事务**里读取窗口、摘要、状态；
  状态来源无法重新证明时只失效语义状态，仍返回已授权窗口。
- `finish_turn(..., dialogue=run)` 在既有 `BEGIN IMMEDIATE` 内比较 `context_version` 并 CAS 写入状态；
  冲突或状态超限会回滚整轮回答，不会留下“答案在、语义没提交”的半成品。
- 内容版本每个内容事务只递增一次：新问题、导入、单独 append、失败轮次都会推进；
  `begin_request` 同时拒绝同会话另一个存活请求（`REQUEST_IN_PROGRESS`）。
- 摘要边界改为按本会话真实有序记录计算（`digest_candidates`）：全局自增 ID 有空洞时
  既不会重复吸收，也不会跳过更早的未摘要轮次。

### 5.5 患者入口（PostgreSQL）

- `0009_conversation_dialogue_state.sql` 增加 `dialogue_state jsonb`（object，≤8192 字节）、
  `dialogue_revision`、`context_version` 与消息 `answer_type`，并加身份列不可变触发器。
- 运行角色只获得三列 `SELECT/UPDATE` 与 `answer_type` 的 `SELECT/INSERT`；
  启动 preflight 对 `conversations` 改成列扫描，其他列或表级 `UPDATE` 仍拒绝启动。
- `patient_dialogue.read_snapshot/commit_state/bump_context` 只在已授权的
  `PatientService._operation` 事务内工作：`FOR SHARE` 读窗口、`FOR UPDATE` 锁会话、按 `context_version` CAS。
- 患者调用 `/answers` 时携带后端构造的有界 `dialogue_context`，且 `memory: false`；
  带上下文的运行不写普通历史、长期记忆、会话向量或共享 `RagService.runs` 缓存。
- 生成期间发生 append 或另一轮作答 → `CONVERSATION_CONTEXT_CHANGED`（409），
  不保存过期答案，前端固定提示「会话已更新，请重新发送问题。」。

### 5.6 v7 工作流

```
query.input → question.resolve ─┬→ dense / bm25 / hyde → rrf → rerank → corrective → gate
                                ├→ answer.plan
                                └→ generation.llm（附带 original_question）
                                        ↓
                              citation.validate → answer.output
```

- 检索、重排、规划、生成全部使用同一个 `resolved_question`；用户原话只作为
  `original_question` 附件供核对原意，不能把对象改回旧主题。
- 澄清在 `question.resolve` 直接 `Halt`，不进入检索、重排、规划或生成。
- v7 纠错只用受控意图同义词扩展检索表达，第二轮重排仍用固定语义问题；v6 保持开放式重写。
- `medical-answer.v7.json` 已发布为 `published`（v6 同时保持 published 以便回滚）；
  运行哪个版本仍只由 `DIALOGUE_STATE_ENABLED` 决定，关闭时显式选择 v6。

### 5.7 评测（`dialogue_evaluation.py`）

- 固定 40 组合成对话、每组 3–6 轮、恰好 10 组含必须澄清轮次：
  `data/evaluations/dialogue-followups-v1/cases.json`。
- `replay` 模式只验证契约，不调用模型，也不代表真实语义质量；
  live 模式必须显式 `--mode live --allow-paid-model`，否则在构造模型客户端前拒绝。
- 指标只记录 case ID、布尔判据、耗时分位与调用次数，不记录问题、实体名或模型输出正文。

### 5.8 部署顺序与回滚

启用顺序（两步都必须由操作员显式执行）：

1. 应用 `database/migrations/0009_conversation_dialogue_state.sql`，再按最小权限执行
   `database/patient_runtime_grants.sql`（文件头迁移上限已是 0009）。
2. 用 `security_server` 的 schema/权限 preflight 验证运行角色（缺列或宽授权都会拒绝启动）。
3. 在**两个进程**注入同名环境变量 `DIALOGUE_STATE_ENABLED=true`：本地问答服务与患者服务。
   患者服务只读进程环境，不读 `.env`，也不在 TOML 里新增字段。
4. 安装已发布制品 `medical-answer.v7.json`（本仓库中 `status` 已是 `published`）。
   不要用 `WorkflowRepository.save` 覆盖同版本（其 API 拒绝覆盖任何已有版本）；
   已发布内容的修正需要新版本号与选择配置评审。
5. 灰度观察：`resolve` 节点的 `status`/`relation`、版本冲突数、解析耗时与调用次数；
   不记录原问题、状态 JSON 或模型响应正文。

回滚：关闭 `DIALOGUE_STATE_ENABLED`（两个进程一起）并固定 v6。应用会显式选择 v6，
任何显式请求 v7 的调用返回 409 `DIALOGUE_FEATURE_DISABLED`。注意 v7 本身仍是
published，回滚靠开关而不是删文件或改 `.index.json`（后者不是开关）。回滚**不删除**
新增列、不清空会话、不回滚迁移：
派生状态会保留，重新启用时以窗口重建。功能关闭期间写入的轮次只会推进
`context_version` 并把状态标记为需重建，绝不会被当成已理解的新主题。
