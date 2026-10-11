# 解耦节点契约设计（自定义工作流）

目标：让画布上的节点可以真正自由增删与重组，检索前处理（重写／分解／HyDE／stepback）支持三种形态——**动态多路路由**、**静态单路**、**完全不做提前检索**——三者都是同一套节点表下"连线不同"而已，不需要改 Python。

本文只定义契约与迁移方案，不含实现。行号锚点为写稿时的 `src/medical_rag/workflow/*`、`frontend/src/*`。

## 1. 验收标准

设计完成后必须能实际通过 `POST /workflows/{id}/validate` 发布并跑绿的三张图：

| 形态 | 图 | 期望 trace |
| --- | --- | --- |
| 动态路由 | `run.input → question.router → {rewrite,decompose,hyde,stepback} → merge.first_active → retrieval.multi → fusion.rrf → retrieval.rerank → evidence.gate → generation.llm → citation.validate → run.output` | 四个变体节点里恰好 1 个 `success`，其余 `inactive` |
| 静态单路 | `run.input → question.decompose → retrieval.multi → retrieval.rerank → evidence.gate → generation.llm → citation.validate → run.output` | 无 router，无 `inactive` |
| 不做预处理 | `run.input → retrieval.dense → evidence.gate → generation.llm → citation.validate → run.output` | 无重排、无 fusion，仍发布成功 |

第三列是关键判据：今天这三张图**一张都发不出去**，原因见下节。

## 2. 现状：五条独立的硬约束

1. **全部输入端口必填**。`validator.py:169-178` 对每个声明端口都要求有入边，否则 `MISSING_INPUT`；`contracts.py:63` 的端口契约只有"名字→类型"，没有可选性。→ 静态单路、不预处理两种形态被否。
2. **路由语义在节点内部**。`nodes/question.py:81-90` 用 `inputs["route"]` 与 `self.type` 尾段比较决定是否跳过，`question.py:8` 的词表 `direct|rewrite|decompose|hyde|stepback` 是 router 与四个变体节点之间的隐式全局约定，图上不可见；被跳过的分支还要伪造一个 `{"active": false}` 占位输出（`question.py:86-89`、`engine.py:199-202` 专门为此放行）。→ "路由"无法与"检索方法"解耦。
3. **收敛／发散形状写死**。`bootstrap.py:57-60` 的 `question.select` 有四个以分支名命名的必填端口；`nodes/retrieval.py:21-30` 的 `rrf` 端口就叫 `dense`/`bm25`，最多两路；`nodes/retrieval.py:50-73` 的 `expanded` 把"多查询 ×(dense+bm25)+RRF"整块吞进一个节点。→ 不能合并两种方法、不能加第三路、不能表达一对多扇出。
4. **类型只容同名相等，且一个端口只许一条入边**。`graph.ts:141-152` 要求 `from === to` 并拒绝多边进同一目标端口，`validator.py:154-165` 同样按字符串全等判类型。私有类型 `query_variant`／`query_plan` 正是为迁就第 3 条那两个大节点而存在的，它们使 HyDE 的输出接不进 `retrieval.dense`。→ 类型系统本身挡住了组合。
5. **安全校验按具体类型名匹配**。`validator.py:214-233` 要求答案型图里必须存在 `evidence.gate`、`citation.validate`、`generation.llm`、`retrieval.rerank` 这四个确切类型，且 `gate.evidence` 的直接上游类型必须是 `retrieval.rerank`。→ 问答主链上四个节点删不掉、换不掉、不能串两次重排。

另有两处直接对应"输入参数与返回结果的限制"：运行入参按三个硬编码工作流 ID 校验（`service.py:74-82`），`provider != "qwen"` 在 `nodes/input.py:13-15` 抛错；结果侧 `engine.py:211-212` 是最后一个 output 节点整体覆盖、`engine.py:229-230` 拒答结果再覆盖一次，`nodes/output.py:6-8` 原样透传，对外结构由生成器决定而非图决定。

顺带一条放大上述所有问题的因素：**配置被当数据连线**。`workflows/medical-answer-expanded.v1.json` 13 节点 31 边，其中 12 条纯粹在搬 `provider`／`route`／`top_k`（`bootstrap.py:32-37、71-74` 把它们声明为端口）。

## 3. 新节点契约

```python
@dataclass(frozen=True)
class PortSpec:
    name: str
    dtype: str                  # 见 §4 类型表
    optional: bool = False      # 未连线 = 不传，节点用 inputs.get(name)
    collect: bool = False       # 允许多条入边，按拓扑序聚合
    requires_tag: str | None = None   # 该端口只接来自含此标签路径的值

@dataclass(frozen=True)
class NodeSpec:
    type: str; title: str; category: str
    inputs: dict[str, PortSpec]
    outputs: dict[str, PortSpec]
    tags: frozenset[str]        # 例：{"evidence.gated"}
    config_schema: dict[str, object]
    shared_producers: tuple[tuple[str, str], ...] = ()  # 例：(("draft","evidence"),)
```

`Node` 协议与 `registry.metadata()` 同步扩展，`GET /nodes` 多返回 `optional/collect/requires_tag/tags`，编辑器据此渲染。

节点实例层新增两个可选字段（写在 `WorkflowNode` 上，不是类型上）：

```json
{"id": "rewrite", "type": "question.rewrite",
 "activation": {"port": "route", "equals": "rewrite"},
 "config": {}}
```

- **activation 由引擎通用求值**：取完入参后判定，不满足则节点状态 `inactive`、**不发布任何输出**。`RoutedQuestionNode`（`question.py:81-90`）、占位 `{"active": false}` 对象、以及 `engine.py:199-202` "显式跳过仍可发布输出"这条特例一并删除。router 由此退化为一个普通节点——留着它=动态路由，删掉它=静态单路，什么都不接=无预处理。路由词表不再需要与节点类型同名。
- **halt 取代 `abstained` 特判**：`NodeResult(status="halt", outputs=...)` 表示"剪掉我的下游子树，并把这份 outputs 作为本次运行结果"。引擎不再认识 `abstained` 字段和 `generation` 分类（`engine.py:214-227`）。`evidence.gate` 把"证据为空则拒答"表达成一次 halt，行为等价。
- 缺失必填输入的**静默跳过**（`engine.py:138-154`）取消，与 §5 的发布期校验保持唯一口径；运行期只有 activation 能造成不执行。
- `engine.py:125-126` 的 `values[0] if len(values)==1 else values` 折叠规则取消：非 collect 端口由校验保证只有一条入边，collect 端口恒为 `list`。多入边不再随数量改变值的形状。

联网能力保持现状：`ExecutionContext.capabilities` + `supports()`（`contracts.py:10-29`）仍是全局内置能力，**不**改造成可插拔节点，见 README「联网补充检索」。

落地时的两处收窄：`/nodes` 的输入端口序列化为 `{dtype, optional, collect, requires_tag}`，不带 `name`（键名已经是端口名）；`activation` 只支持 `equals` 单值，多值匹配没有实际场景，等真需要再加。

## 4. 类型表与唯一的加宽规则

```
text  text_list  selector  evidence  evidence_list  answer_draft  answer  citation_list  bytes  document  chunks  vectors  ingest_result  parsed_document
```

- 兼容判定：`T == T`，或任一侧为 `any`（仅用于 `run.output` 之类需要透传的场景）。
- **唯一的加宽**：`collect` 端口（其声明类型为 `T`）接受来自 `T` 或 `list<T>` 的入边，引擎按序展平一层。这是让"分解结果喂多查询检索"成立的最小代价，且只在 collect 端口生效。
- 不做任何隐式标量↔列表转换：形状变化一律用显式节点（`wrap.single`、`merge.collect`、`split.items`），这样每一次形状改变都在画布上可见——这正是解耦要的语义。
- 废除 `query_variant`、`query_plan`。`question.rewrite/stepback/hyde → text`，`question.decompose → text_list`。HyDE 的产物本来就只是"一段拿去 embedding 的文本"，`retrieval.dense(query: text)` 可以直接接。

## 5. 校验规则重设计

`MISSING_INPUT` 只对 `optional=False` 的端口报（改 `validator.py:169-178`）。医疗安全底线**保留但放宽**，换成两条图可见的声明式规则：

1. **必经标签（路径支配）**：端口声明 `requires_tag` 时，校验该消费节点在 DAG 上的**必经点**中存在携带该标签的生产者。算法用 Cooper–Harvey–Kennedy 支配集在拓扑序上求交（`dom(n) = {n} ∪ ⋂ dom(preds)`），所有 input 节点挂一个虚入口。据此：
   - `generation.*.evidence` → `requires_tag = "evidence.gated"`
   - 输出答案 schema 的 `run.output.answer` → `requires_tag = "answer.cited"`
   违反时仍报 `UNSAFE_ANSWER_FLOW`，但不再引用任何具体类型名。于是：重排换成 `fusion.rrf` 直喂门控、串两次重排、自定义门控节点都合法；"生成必经证据门控、答案必经引用校验"依旧守死。
2. **同源证据（边的性质，不是支配性质）**：`citation.validate` 声明 `shared_producers = (("draft","evidence"),)`，校验 `draft` 与 `evidence` 两个端口的**直接生产者必须是同一个节点实例**。这条专门保住 README:70 记下的真实教训——网页证据只存在于生成节点的输出里，引用校验若接门控的证据就会漏掉联网块，`medical-answer.v1/v2` 的差异就是这个案例。放宽标签校验后，必须靠这条把它补回来。

固定节点（`contracts.py:15-19`、`graph.ts:73-94` 的自动补回、`WorkflowEditorPage.tsx:214-235` 的删除拦截）保留，但只保留"至少一个 input、至少一个 output"这一条最小不变量；被固定的是**类别**而不是具体类型名，`CANONICAL_FIXED_TYPES` 指向新的 `run.input`／`run.output`。

## 6. 节点表改造清单（`bootstrap.py`）

| 现有类型 | 处置 | 新契约要点 |
| --- | --- | --- |
| `query.input` | 替换为 `run.input` | 输出端口由 `config.params` 声明，见 §7 |
| `question.router` | 保留 | 输出 `route: selector`；无任何节点把它设为必填 |
| `question.rewrite/stepback/hyde` | 去 route 端口 | 输出 `text`；`hyde` 不再是私有类型 |
| `question.decompose` | 去 route 端口 | 输出 `text_list` |
| `question.select` | **删除** | 由 `merge.first_active`（`candidates: collect text (optional)` → `text`）与 `merge.collect`（→ `text_list`）替代 |
| `retrieval.expanded` | **删除** | 拆为 `retrieval.multi`（`queries: collect text`，config `strategy: dense\|bm25\|hybrid`、`k` → `evidence_list`） |
| `retrieval.rrf` | 端口改造 | `sources: collect evidence_list`，任意路数 |
| `retrieval.dense/bm25` | 去 `top_k` 端口 | `in: query: text`，`top_k` 降到 config |
| `retrieval.rerank` | 去 `top_k` 端口 | `in: query: text, evidence: evidence_list`；`top_k/rerank_k/threshold` 全在 config |
| `evidence.gate` | 保留 | 空证据改为返回 `halt`；`tags={"evidence.gated"}` |
| `generation.llm` | 去 `provider` 端口 | provider 走运行上下文；同时输出 `draft` 与 `evidence`（供 §5 规则 2） |
| `citation.validate` | 保留 | `tags={"answer.cited"}`，`shared_producers=(("draft","evidence"),)` |
| `answer.output` / `search.output` | 合并为 `run.output` | 声明输出 schema 与结果键名，见 §7 |
| `document.*`（parse/chunk/embed/store）、`document.input` | 端口可选性补齐 | 摄取链形状本来就线性，不重构 |

删掉 `RoutedQuestionNode`（定义在 `nodes/question.py:81-90`），四个变体回到 `nodes/base.py` 的普通 `BuiltinNode`。

### 6.1 P2 落地时的偏离与理由

- **dtype 命名沿用现有词表**（`string`/`boolean`/`integer`/`evidence_list`/`answer`/…），没有改成 §4 草案里的 `text`/`evidence`；新增的是 `selector`，集合类型统一写成 `list<string>` 这种 `list<T>` 形式。理由：与 `config_schema` 已有的 JSON 类型名保持一致，改动面小；而引擎/校验器的"一层展平"规则只认 `list<…>` 语法，所以集合类型必须用这个形式，`evidence_list` 则继续当作不可拆的原子类型（融合节点要的是"每条检索结果的列表"，不是把它们拍平）。
- **`provider` 和 `top_k` 先标成 `optional` 端口，没有直接删**。`/answers` 的运行级 `top_k` 仍要能影响重排结果数，而它的最终归宿是 P3 的运行上下文（`run.input` 参数）；这一期先把"必须连线"松开，图里不接也能跑（`retrieval.rerank` 的取值优先级是 节点 config > 连线入参 > 服务默认值）。
- **activation 引用的端口必须在节点上声明**（否则校验器无从静态检查），所以四个变体各多了一个 `route: selector (optional)` 端口。router 的 `selector` 输出与之同名，静态类型检查因此还能挡住"把任意 string 接进路由口"。
- **没有实现 `merge.first_active` 与 `split.items`**：路由形态下未激活的分支根本不发布值，`merge.collect`（optional + collect）一个节点就同时覆盖了"任选其一"和"任意合并"，多一个节点只是多一个概念。
- **`retrieval.rrf` 保留原类型 id**，只把 `dense`/`bm25` 两个端口换成 `sources: collect evidence_list`；`retrieval.expanded` → `retrieval.multi`（`queries: collect string` + `strategy` 配置），`question.select` 删除。
- **`skipped` 不再有权发布输出**（原 `engine.py:199-202` 的特例随 `RoutedQuestionNode` 一起删除）：skip 就是"这次没有值"，下游用 optional/collect 吸收缺口。

## 7. 运行入参与返回结果改由图声明

- `WorkflowDefinition` 增 `run_params`（由校验器从 `run.input` 的 `config.params` 推导并缓存，不落两份真值）：每项 `{name, dtype, required, default}`。
- 新增 `GET /workflows/{id}/run-schema`；`service.py:74-82` 的 ID 白名单整段删除，`run_workflow` 改为按推导出的 `run_params` 做通用类型校验。加一个自定义变量从此只是编辑器里的一次表单操作。
- `run.input` 取代 `nodes/input.py:6-16` 的固定三字段校验；`provider` 从数据端口移到运行上下文（`ExecutionContext.runtime`），因为它不是流经过图的数据，而是本次调用的配置。`input.py:13-15` 的硬编码厂商判断改成从 `settings` 取允许集合。
- `run.output` 声明 `{name, schema}`，引擎把各输出节点按键名合并进结果（替换 `engine.py:211-212` 的最后写入覆盖），halt 结果作为同名键覆盖。
- 问答页兼容性：`frontend/src/workflow/selection.ts:24-54` 目前靠五个确切类型名 + 一条链判"这是不是答案型工作流"。改成按声明判：存在一个 `run.output` 实例，其 schema 含 `answer: string` 与 `citations`，且路径满足 §5 的两条规则。否则自定义工作流一旦改名或换门控就从问答页消失。

## 8. 前端影响

- `graph.ts:125-166` `compatible()`：类型判定接 §4 规则；collect 端口放行多边（现在第 148-152 行直接拒绝）；`WorkflowNode.tsx:38-57` 把 optional 端口渲染成空心把手、collect 端口标 `…`。
- `graph.ts:14-20、73-94`：`fixedSiblings`／`withFixedNodes` 只针对 `run.input`／`run.output`。
- 属性面板新增 activation 编辑（选一个入边端口 + 期望值），这是"保留路由／不路由"在 UI 上的唯一入口。
- 运行面板改为按 `run-schema` 动态生成表单。
- 现状提醒：`frontend/node_modules` 不存在，`npx vitest` 报 `ERR_MODULE_NOT_FOUND`，UI 改动前需先 `npm --prefix frontend install`，否则前端无法自测。

## 9. 就地重写内置工作流的影响

按已定方案，`workflows/` 下 4 个 JSON 直接改写到新契约、不另存版本。需要一并处理两件已知后果：

1. README:50 明确把 `medical-answer.v1` vs `v2` 当作"证据流向影响"的对照教材（v1 的引用校验没接生成节点的证据，所以联网那次仍会拒答）。**P1 已按 §5 规则 2 落地处理**：v1 保持原接线但降为 `draft`，`/workflows` 不再列出它，引擎仍可直接执行它来复现"联网检索到了补充证据、结论依旧被拒"；同时新增测试 `test_history_graph_is_rejected_for_splitting_draft_and_evidence` 断言这种接线的校验结果是 `["PROVENANCE_MISMATCH"]`。这条对照从"可运行的历史版本"变成了"校验器直接拒绝的反例"，README:50 已同步改写。
2. 版本号语义：`repository.py:79-83` 禁止覆盖已存在版本。就地重写 `v1.json`/`v2.json` 文件内容可行，但 `repository.save()` 这条不可覆盖规则意味着**运行期新存的版本仍要递增**；文档与 README:64 关于"部署时保持 `workflows/` 可写"的说明不变。

## 10. 分期与实测成本

迭代回路实测：`python -m pytest tests/test_workflow_validator.py tests/test_workflow_engine.py tests/test_workflow_nodes.py tests/test_question_nodes.py -q` → **56 passed / 4.30s**（不需要加载模型，后端改动可秒级验证）。

需要重写断言的测试文件：`test_workflow_validator.py`(303)、`test_workflow_engine.py`(705)、`test_question_nodes.py`(123)、`test_workflow_nodes.py`(374)、`test_expanded_retrieval.py`(43)、`test_routed_workflow_api.py`(100)、`test_workflow_api.py`(205)；前端 `test/graph.test.ts`(154)、`test/editor.test.tsx`(424)。

- **P1 契约层（已完成）**（`contracts.py`/`definition.py`/`engine.py`/`validator.py`/`registry.py`/`nodes/base.py`/`nodes/evidence.py`）：PortSpec、activation、halt、collect 语义、支配集标签校验、`shared_producers`。实测结果：`python -m pytest -q` → **341 passed, 1 skipped / 37.5s**，`ruff check src tests` 干净；前端 `npx vitest run` → **56 passed / ~35s**，`tsc --noEmit` 与 `eslint src --max-warnings=0` 干净。节点表除标签声明外未动，四张内置图里有三张（v2／expanded／search／ingestion）继续零问题通过校验，v1 见上条。
  - `evidence.gate` 的拒答在本期就切到了 halt：引擎里"任何节点输出 `abstained: True` 就按 generation 分类剪枝"的旧特判被删除，若把这条留到 P2，会同时存在两套控制流机制。
  - `requires_tag`／`shared_producers` 也在本期接到了 `generation.llm`、`answer.output`、`citation.validate` 上，`validator.py` 里按类型名匹配的 `UNSAFE_ANSWER_FLOW` 旧块随之删除。
- **P2 节点重组（已完成）**（`bootstrap.py` + `nodes/question.py`/`nodes/retrieval.py`/`nodes/merge.py`/`nodes/generation.py` + `workflows/*.json`）：按 §6.1 的偏离记录拆表，废 `query_variant`/`query_plan`，4 张内置图就地重写，新增 `medical-answer-decompose.v1.json`（静态单路）与 `medical-answer-direct.v1.json`（不提前检索、不重排不融合）。§1 三种形态实测：三张图都通过发布校验；分解图运行时 `search_dense` 被调 3 次（原问题 + 两个子问题）且答案 `abstained` 为假，直连图 1 次；路由图由 `tests/test_routed_workflow_api.py` 覆盖（未选中分支 trace 为 `inactive`，模型调用数 direct=2／其余=3）。回归：`pytest -q` → **345 passed, 1 skipped / 28.4s**，`ruff check src tests` 与 `compileall` 干净；前端 **56 passed**，`tsc --noEmit`、`eslint` 干净。
- **P3 图声明入参/出参 + 编辑器（部分完成，剩下的是一整块独立工作）**：
  - 已完成：`service.py` 的三 ID 硬编码白名单删除，入参校验落到 `query.input` 节点（抛 `ApiError("INVALID_QUERY"/"INVALID_PROVIDER", 422)`），服务层按错误码把请求类失败映射回 422，模型类失败仍保持"200 + trace"的既有语义；新增 `workflow/run_schema.py` 与 `GET /workflows/{id}/run-schema`，运行入参由图推导（输入节点的输出端口即字段，`required` 看它有没有被下游连线用掉）；编辑器属性面板新增「执行条件（路由）」编辑区，`optional`/`collect` 端口有了空心把手与 `⇉` 标记，`inactive`/`halt` 两种 trace 状态有了配色；`selection.ts` 无需改动——它按"输入→检索→门控→生成→引用→输出"的路径判定答案型工作流，新增的 decompose／direct 两张图实测仍在列表内。
  - 未完成（§7 的后半）：**用户自定义运行参数**（`run.input` 的 `config.params` → 实例级输出端口）与 **输出结果声明**（`run.output` 的 schema + 按键名合并，替换 `engine.py` 的最后写入覆盖）。这两件依赖同一处改造：把"节点的端口"从类型级降到实例级，牵连 `validator` 的端口查找、`engine._collect` 的源端口类型、`registry`/`/nodes` 的目录形态、`graph.ts` 的连线兼容判定、`config_schema` 对数组/对象类型的校验（现在只认标量，`params` 会被判 `INVALID_CONFIG`）、`CANONICAL_FIXED_TYPES`、5 张内置图与 `selection.ts` 的类型名。它本身是一个完整的改动单元，不适合拆半提交。
  - 实测：`pytest -q` → **346 passed, 1 skipped / 36.6s**，`ruff`＋`compileall` 干净；前端 **57 passed**，`tsc --noEmit`、`eslint --max-warnings=0`、`npm run build` 均通过。

P1+P2 完成后，你说的"自由选择不同方法进行提前检索／不路由／不提前检索"即可全部实现；P3 才解决"输入参数与返回结果被写死"。

## 11. 本设计明确不做

- 通用子图／map-reduce 迭代（每条子查询走不同检索策略）。P2 用 `retrieval.multi` 覆盖"多查询同一策略"，异构扇出留待后续。
- 把联网补充改成可插拔节点（保持全局能力，README:66-72 的语义不变）。
- 环、循环、条件回边等非线性执行。
- 多用户鉴权与工作流权限模型。
