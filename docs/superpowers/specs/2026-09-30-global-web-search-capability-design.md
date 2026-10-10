# 联网搜索内置为全局能力设计

## 背景

联网补充检索此前是**节点配置**：已发布的 `medical-answer` v2 在 `evidence.gate` 上写 `{"allow_web_search": true}`、在 `generation.llm` 上写 `{"web_search": true}`，节点工厂 `build_registry()` 还在构造时把 `web_search_available = bool(generator.tools)` 绑到门控的 `partial` 上。结果是同一份代码里联网能力被表达了两次——一次是运行时的工具可用性，一次是工作流 JSON 里的布尔字段——并且只有显式打开这两个字段的工作流（当时只有 v2）才能联网；`medical-answer-expanded` 虽然也接好了生成节点，却因为没有打开字段而完全无法触网。

系统的设计目的是通过编辑和组合节点来对照不同组合对检索结果的影响。联网检索属于"生成阶段能否取到外部证据"这一基础能力，不是某条工作流的可插拔步骤；把它做成节点开关，等于要求每条新工作流都重复声明一次同样的东西，且漏写时的失败方式是静默降级（检索落空直接拒答），难以观察。

## 目标

- 联网检索成为**每条工作流都具备的全局内置能力**：新工作流无需任何配置即可联网，也不存在"忘记打开"的状态。
- 节点配置面不再出现联网开关：`/nodes` 不再返回 `web_search` / `allow_web_search`，工作流 JSON 不写这两个字段。
- 能力的唯一开关是环境：`TAVILY_API_KEY` 配好即生效，留空即完全关闭，行为与不联网时逐字节一致。
- 保留"检索落空时不再过早拒答"与"检索清晰时不触网"这两个既有语义。

## 非目标

- 不新增节点类型（不把联网做成一个"联网检索节点"）。联网命中的证据仍然只存在于生成节点的输出端口，不落库、不参与检索指标。
- 不改自检策略、Tavily 调用、错误码映射或引用校验规则。
- 不改 `/answers`、`/search`、`/documents` 的对外契约，也不改工作流 JSON 的 Schema（节点仍然只有 `id` / `type` / `position` / `config`）。

## 设计

### 能力声明与注入

`src/medical_rag/workflow/contracts.py` 定义能力名与读取入口：

```python
CAPABILITY_WEB_SEARCH = "web_search"

def supports(capabilities: dict[str, bool] | None, name: str) -> bool:
    return bool((capabilities or {}).get(name))
```

`ExecutionContext` 增加 `capabilities: dict[str, bool] = field(default_factory=dict)`，位置在 `variables` 之后、`trace` 之前。默认空字典让所有既有构造点（测试、节点单测）继续可用，缺省即"无能力"。

`RagService.__init__` 声明能力并交给引擎：

```python
def global_capabilities(generator: object) -> dict[str, bool]:
    return {CAPABILITY_WEB_SEARCH: bool(getattr(generator, "tools", None))}
```

`WorkflowEngine(registry, capabilities)` 在 `run()` 里把能力复制进本次运行的 `ExecutionContext`。能力在服务启动时确定一次，与工作流定义无关：同一进程内所有工作流看到同一份能力，符合"最基本功能"的定位。

工具集本身仍由 `build_tools(settings)` 决定：`TAVILY_API_KEY` 为空时返回 `None`，能力为 `false`，行为与不联网时完全一致；配好密钥时 `GroundedGenerator.tools` 非空，能力为 `true`。

### 节点读取

节点执行时读取上下文能力，不再从 `config` 读开关：

- `evidence.gate`：过滤后无证据时，`supports(context.capabilities, CAPABILITY_WEB_SEARCH)` 为真则不置 `abstained`，把控制权交给生成阶段；为假则维持原有拒答（`ABSTENTION`）。该节点的 `config_schema` 只剩 `threshold`。
- `generation.llm`：把能力原样传给 `GroundedGenerator.generate(..., web_search=…)`。该节点的 `config_schema` 为空。

`GroundedGenerator` 侧不做改动：`web_search and self.tools` 仍是最终闸门，因此能力为真但工具缺失时自动退回纯本地路径。

### 工作流接线

网页证据只出现在生成节点的 `evidence` 输出里，因此**引用校验的证据端口必须接生成节点**，否则联网取回的块进不了逐字校验（生成阶段的草稿会被全部判为伪造引用，最终仍拒答）：

- `medical-answer` v2：已经是 `llm.evidence → citation.evidence`。
- `medical-answer-expanded` v1：由 `gate.evidence → citation.evidence` 改为 `llm.evidence → citation.evidence`。这是让全局联网在该工作流真正生效的必要修复，节点集合与版本号不变。
- `medical-answer` v1：保持 `gate.evidence → citation.evidence` 不动。它会发起联网检索，但补充证据不参与校验，输出仍为拒答——作为"证据流向决定联网结果能否落地"的对照版本保留。

工作流 JSON 里删除 `{"allow_web_search": true}` 与 `{"web_search": true}`；`medical-answer` v2 的名称回到"医疗资料问答"，因为联网不再是该版本的特征。

### 兼容性

`WorkflowNode.config` 由节点 `config_schema` 校验：旧工作流若仍带这两个字段，`validate_workflow()` 报 `INVALID_CONFIG`（未知配置字段）并拒绝发布。这是有意的失败方式——与其静默忽略一个已经不起作用的开关，不如让保存动作明确失败。仓库内的四个已发布工作流已同步清理。

## 错误与降级

- 能力为 `false`：门控在无证据时拒答，生成节点不提供工具，与不联网路径一致。
- 能力为 `true` 但 Tavily 未配置工具：不可能出现（能力正是由工具集推导），即使人为注入也为 `web_search and self.tools` 拦下。
- Tavily 故障：沿用 `WEB_SEARCH_*` 错误码与"失败只作为错误负载回给模型"的既有处理，不中断本次回答。
- 联网取回的网页块：`citations[].origin = "web"`，不替代医生诊断。

## 测试

`tests/test_web_search_capability.py`：

- 所有已发布工作流的节点配置都不含 `allow_web_search` / `web_search`；门控 Schema 只剩 `threshold`，生成节点 Schema 为空。
- 校验器对带节点级联网开关的工作流报 `INVALID_CONFIG`。
- `supports()` 对缺省、`None`、显式 `false` 都返回假。
- `medical-answer` 与 `medical-answer-expanded` 在不写任何节点配置的前提下，装配 Tavily 工具后都能联网作答并产出 `origin = web` 引用。
- 门控只在能力生效时把空证据交给下游。
- `RagService` 由工具集推导能力并注入引擎；没有工具的生成器对应能力为假。
- 每条含生成节点的工作流，其引用校验的证据来源都是生成节点。

`tests/test_workflow_nodes.py` 同步调整：联网用例显式构造带能力的引擎；`medical-answer` v1 用例改名为 `test_published_history_keeps_its_own_evidence_wiring`，断言"能发起检索但结论仍被拒"这一接线差异。

## 文档

`README.md` 的"联网补充检索"一节改为先说明全局能力（声明位置、注入路径、读取方式、开关是环境变量、旧字段会被校验拒绝），再说明节点侧的两处读取与自检三态；"工作流与前端"一节说明 v1 与 v2 的差异已变成证据接线差异，而不是联网开关差异。
