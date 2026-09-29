# 生成阶段联网补充检索设计

## 目标与范围

问答的生成阶段在检索不足时通过 Tavily 联网补充上下文，并把 `tavily_search` 作为**可注册的工具**接入，而不是固定的流程节点，为后续生成 agent 增加更多工具（短期记忆、human in the loop 等）留出同一接入点。联网命中与本地检索块一样进入提示词并参与逐条引用校验，来源标注为 `web`。默认流程 `medical-answer` 的节点连线与拒答行为保持不变；能力通过新增的已发布流程 `medical-answer-websearch` 与节点配置启用，`TAVILY_API_KEY` 未配置时工具集为空、行为与不联网完全一致。

## 自检的三种情况

`selfcheck.assess()` 在生成前判定检索质量，只依据已有信号，不额外调用模型：

- `none`：门控后没有任何证据块。强制以原问题联网检索，用网页内容兜底生成。
- `vague`：有证据但不足够——最高重排分低于 `CLEAR_EVIDENCE_THRESHOLD`，或条数少于 `MIN_CLEAR_EVIDENCE`。先以原问题检索，再允许模型自行改写检索词补充。
- `clear`：分数与条数都达标，直接依据本地检索作答，不提供也不调用工具。

三种情况都要求节点配置开启（生成节点 `web_search`、门控 `allow_web_search`）且工具可用，否则一律走本地路径。

## 工具接入与提示词

`tools.py` 定义 `Tool`（名称、说明、JSON Schema、handler）与 `ToolRegistry`（`specs()` 输出 OpenAI function-calling 结构、`execute()` 返回给模型的文本负载加本次新证据）。`tavily_search` 只暴露一个 `query` 参数并禁止额外字段。`OpenAITransport.chat()` 承载工具轮次：模型请求工具时，按 OpenAI 协议把 assistant 的 `tool_calls` 与每条 `tool` 消息写回 `messages`，因此网页摘录与本地证据块一同构成最终提示词；轮次预算由 `MAX_AGENT_TOOL_CALLS` 限制，预算耗尽后不再提供工具、要求模型直接作答。

## 证据、引用与门控

网页命中转换为 `Chunk`/`Evidence`：块 ID 为 `web-` 加 URL 摘要、`source_id` 与 `section` 存 URL、`filename` 存标题、正文按 `WEB_CONTENT_MAX_CHARS` 截断、同一 URL 去重、`origin="web"`。`citation.validate` 合同不变，excerpt 必须能对应回相应块，所以联网引用同样无法伪造；引用对象新增 `origin`，前端与调用方据此区分网页来源。生成节点除 `draft` 外输出本次**有效证据集**，新流程把 `citation.validate` 的 `evidence` 端口从门控改接生成节点，保证校验看到的证据与提示词完全一致。门控在检索为空但允许联网兜底时不再立即拒答，把兜底交给自检；若联网仍无可用证据，生成器不调用模型，引用校验返回既有拒答文案。`UNSAFE_ANSWER_FLOW` 的四条不变量全部保留：门控证据仍只能来自重排、生成证据仍只能来自门控、草稿仍只能来自生成节点、答案仍只能来自引用校验。

## 真机联调带来的两项校准

首版按 `CLEAR_EVIDENCE_THRESHOLD=0.75` 上线后，实测本语料的相关题最高重排分只有 0.726–0.73，无关题约 0.50——0.75 会把"已经清晰"的检索误判为模糊、每题都触网，因此默认值下调为 **0.65**，换语料后仍需以评测集重新校准。其次，PDF 抽取正文带换行，模型逐字复制时会重新折行，严格的字节级 `excerpt in text` 判定导致**所有回答被拒**；沿用主工作区已验证的 `_verified_excerpt`：先按原文命中，否则在忽略空白的归一化文本上定位并回灌原文切片，单条结论校验不过只丢弃该条、其余仍可作答。同时吸收同一工作区的 Qwen 延迟修复（`enable_thinking=False`、`timeout=120s`、`max_retries=0`），否则带证据的生成会撞穿原 30 s 客户端超时。主工作区尚未提交的 `embeddings.py` 性能优化（重排 `max_length=512`、`bfloat16`）未纳入本分支，联网流程实测 54–58 s，合并后应尽快采用。

## 失败与降级

Tavily 故障映射为 `WEB_SEARCH_NOT_CONFIGURED / UNAVAILABLE / AUTH_FAILED / RATE_LIMITED / INVALID_REQUEST / UPSTREAM_ERROR`，消息不含上游响应体。工具内部的失败以 JSON 错误负载返回给模型，不中断本次问答，已有本地证据时仍能作答。问题为空或全为空白在发起网络请求前拒绝。

## 配置

`.env`：`TAVILY_API_KEY`、`TAVILY_BASE_URL`、`TAVILY_SEARCH_DEPTH`、`TAVILY_MAX_RESULTS`、`TAVILY_TIMEOUT_SECONDS`、`WEB_CONTENT_MAX_CHARS`、`CLEAR_EVIDENCE_THRESHOLD`、`MIN_CLEAR_EVIDENCE`、`MAX_AGENT_TOOL_CALLS`。工作流 JSON 不保存密钥。网页来源仅用于公开资料检索与学习，不替代医生诊断；这不代表引入 URL 摄取能力。

## 验证

`tests/test_web_search.py` 覆盖请求构造、Bearer 与超时/状态码映射、正文截断与去重、工具 Schema 与参数校验；`tests/test_generation_agent.py` 覆盖三种自检情况、工具轮次与预算、联网伪造摘录被拒、搜索失败降级以及无工具时的既有行为；`tests/test_workflow_nodes.py` 覆盖新流程校验通过、检索为空时由联网作答并输出 `origin=web` 引用，以及默认流程即使装配了工具仍按原门控拒答。`httpx` 由开发依赖提升为运行时依赖。
