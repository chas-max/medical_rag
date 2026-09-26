# 公开医疗资料 RAG 问答系统设计

## 目标与范围

构建以中文公开医疗资料为语料的 FastAPI 后端和可编排前端，完成 Markdown、TXT、可提取文字的 PDF 的解析、自动分块、向量化、Milvus 入库、BM25 与向量混合检索、重排和有引用的问答。前端包含普通用户问答页和拖拽式 Workflow 编辑器；不处理患者隐私资料、扫描版 PDF、URL 或图片。回答仅供资料检索参考，不替代临床判断。

## 技术选型与约束

- Python、FastAPI、Pydantic、pytest；采用分层但轻量的组件接口，便于离线测试与未来增加输入类型。
- 稠密向量：本地 `BAAI/bge-small-zh-v1.5`，512 维；文档与问题使用同一模型和归一化策略。模型下载由运行环境负责，测试通过替身避免下载。
- 存储：Docker Compose 启动 Milvus Standalone（版本至少 2.5.11）；稠密字段使用 `HNSW_PQ`、`COSINE`，索引参数显式配置（初值 `M=16`、`efConstruction=200`、`m=64`、`nbits=4`；512 可被 64 整除）；全文字段由 Milvus 内置 BM25 函数生成稀疏向量，使用 `SPARSE_INVERTED_INDEX` 和 `BM25` 度量。启动时验证集合 schema／索引兼容性，不自动删除已有集合；若部署不支持该索引或实际建索引报错，明确返回错误，不静默改用另一种索引。Milvus 对未达到每段索引门槛的小语料可能内部使用暴力扫描；物理索引构建以 `indexed_rows` 验收，不将索引配置等同于已建成。
- 中文全文检索使用 Milvus `chinese` 分析器（jieba 分词）；在集成测试中用医学术语、药名和中文短句检查分词／召回，不假定默认分析器适用。
- 重排：本地 `BAAI/bge-reranker-v2-m3`，配置候选上限和答案证据数；模型失败不绕过证据门槛。
- 生成：DeepSeek `deepseek-flash` 与阿里云百炼 Qwen `qwen3.8-flash` 作为当前默认值，模型 ID、base URL 和 API Key 独立配置，运行时按 provider 显式选择。后续模型变化通过配置处理，不声称永久“最新”。不在仓库保存密钥。

## 组件与数据流

1. `parser` 接收限定格式的文件，进行大小、扩展名及内容校验；TXT/Markdown 解码并规范空白，PDF 按页提取文本并保存页号。无文字 PDF 明确报不支持；不进行 OCR。
2. `router` 根据输入格式及结构选择分块器：Markdown 按标题层级；TXT 如有多个足够长且主题变化明显的段落则走语义分块，否则递归；PDF 对逐页段落作同类判断，在无明显主题变化时递归。路由原因、选用策略写入入库响应，便于检查。语义边界使用相邻段落的 embedding 相似度和可配置阈值，短文本不强行语义分块。所有策略有可配置长度上限与重叠，保留标题、原文件、PDF 页号和块序号；超长标题段落仍递归细分，不丢文字。
3. `embedder` 批量向量化块，检查输出维度、有限数值和长度对应关系。
4. `store` 在一个 Milvus 集合存储块 ID、文档 ID、内容、来源信息、稠密向量及自动生成的 BM25 稀疏向量。文档 ID 根据文件内容哈希生成：相同文件重复上传返回已有结果；同一来源标识的不同内容先完成解析／向量化，再插入新块并确认，最后删除旧块。若插入失败，保留旧块并清理新块；若删除旧块失败，返回明确错误，后续按来源标识重试清理，不报告更新成功。初版串行入库，避免同一来源并发替换竞态。
5. `retriever` 分别执行稠密与 BM25 查询，各取可配置数量；以块 ID 去重并用 RRF 融合（初值 `k=60`），然后让重排器筛选前 N 个。`/search` 返回来源、融合／重排分数和摘录，便于调试。
6. `answerer` 仅在至少一个证据块的重排分数高于可配置阈值时调用模型；将重排分数归一到 0–1，初始阈值为 0.5，并提供本地评估样本以便部署时校准；无法计算可信分数时拒答。向模型提供有编号的可信块和严格的引用格式。每条事实性结论要求引用本次证据 ID；响应结构化为回答与引用，服务端检查非空引用、ID 属于本次结果、引用摘录确实取自对应块。不合规或证据不足时返回固定的资料不足答复，不返回未验证的生成正文。此检查只能降低无出处断言，无法形式化证明语义蕴含；文档与接口须明确这一边界。

## HTTP 契约与错误处理

- `POST /documents`：multipart 上传文件与可选来源标识，同步入库，返回文档 ID、块数、策略／理由、重复状态。
- `GET /documents`：列出已入库文件及基本元数据。
- `POST /search`：传入问题及可选 top_k，返回重排后的证据；空问题或越界参数返回 422。
- `POST /answers`：传入问题、provider（`deepseek` 或 `qwen`）和可选 top_k，返回回答、引用和是否拒答的标志；无证据时不调用生成 API。
- `GET /health`：服务进程状态与 Milvus 可用状态，不泄露密钥。
- 成功与失败统一为 `{success, data, error}`；错误包含稳定 code 和可读 message，使用适当 HTTP 状态码。模型或 Milvus 不可用时不伪造答案；未知 provider、超大文件、不支持格式与无文本 PDF 明确报错。上传限制、超时、候选数等由配置约束。

## Workflow Engine 与节点化设计

固定的 RAG 服务升级为可编排的 DAG Workflow Engine。Engine 负责节点注册、端口和配置校验、拓扑排序、输入汇合、执行状态与运行追踪；解析、检索、重排、生成、引用校验等能力由独立 Node 提供。新增技术或功能通过新增、替换节点或改变连线实现，不修改 Engine 核心。

每个节点实现统一协议：

```python
class Node:
    type: str
    title: str
    input_schema: dict
    output_schema: dict
    config_schema: dict

    async def execute(self, inputs: dict, config: dict,
                      context: ExecutionContext) -> NodeResult:
        ...
```

Node Registry 向 `/nodes` 暴露节点类型、分类、端口和配置 Schema，前端据此生成节点面板和属性表单。首批问答节点为 `query.input`、`retrieval.dense`、`retrieval.bm25`、`retrieval.rrf`、`retrieval.rerank`、`evidence.gate`、`generation.llm`、`citation.validate` 和 `answer.output`；入库节点为 `document.input`、`document.parse`、`document.chunk`、`document.embed`、`document.store` 和 `document.output`。

默认问答工作流为：

```text
query.input → dense / bm25 → rrf → rerank → evidence.gate
             → generation.llm → citation.validate → answer.output
```

默认入库工作流为：

```text
document.input → document.parse → document.chunk
               → document.embed → document.store → document.output
```

工作流以版本化 JSON 保存，包含 `id`、`name`、`version`、`status`、`input_schema`、`nodes` 和 `edges`。状态为 `draft`、`validated` 或 `published`。初版使用项目内 `workflows/` 文件仓储，保存新版本而不覆盖历史版本；未来可替换为数据库仓储而不改变 API。

执行器必须检查未知节点、重复 ID、端口类型、必需输入、孤立节点和环路，然后按拓扑顺序执行。每个节点记录 `pending`、`running`、`success`、`skipped` 或 `failed`、耗时、脱敏摘要和错误。证据门控未通过时跳过生成节点；引用校验失败时返回固定资料不足答复。模型密钥、Milvus 地址和模型名称只在后端配置，不写入工作流 JSON 或前端响应。

新增 Workflow API：

```text
GET    /nodes
GET    /workflows
POST   /workflows
GET    /workflows/{workflow_id}
PUT    /workflows/{workflow_id}
POST   /workflows/{workflow_id}/validate
POST   /workflows/{workflow_id}/runs
GET    /runs/{run_id}
```

运行返回 `run_id`、`status`、最终 `output` 和节点 `trace`。现有 `/search`、`/answers`、`/documents`、`/health` 通过默认工作流适配，保持兼容。

## 双页面前端设计

前端使用 React、TypeScript、Vite 和 `@xyflow/react`，通过 Vite proxy 调用 FastAPI；生产构建后的 `frontend/dist` 由 FastAPI 同源托管。统一顶部导航可在“医疗问答”和“工作流编辑器”之间切换，路由为 `/chat`、`/workflows` 和 `/workflows/:workflowId`。

### 页面一：医疗问答 `/chat`

页面采用 ChatGPT 风格的布局：左侧会话栏、中间消息线程和底部输入框。顶部显示当前工作流版本和服务健康状态，并提供工作流、provider 和 top_k 选择。

页面支持新建会话、本地历史、Enter 发送、Shift+Enter 换行、公开资料上传和文档入库。回答展示 `output.answer`、拒答状态和引用编号；点击引用后显示文件名、页码、原文摘录、检索分数和对应节点。无证据、服务不可用、模型失败或引用校验失败时显示明确状态，不显示未验证生成正文。页面固定提示内容仅供公开资料检索参考，不替代医生诊断、治疗或紧急医疗建议。

### 页面二：工作流编辑器 `/workflows/:workflowId`

页面使用 React Flow，布局为左侧节点库、中间画布、右侧属性面板和底部运行追踪面板。节点库按输入、文档、检索、融合、重排、证据安全、模型和输出分类；节点拖入画布后生成唯一 ID；端口连线按数据类型校验；右侧表单由节点配置 Schema 自动生成。

编辑器支持节点删除、复制、禁用、缩放、平移、自动布局、小地图、工作流验证、版本保存和运行。验证会定位环路、缺少必需输入、缺少输入或输出节点、未知节点和非法配置。运行时节点显示未执行、执行中、成功、跳过或失败，底部展示节点耗时、脱敏输入输出摘要、错误信息和证据预览。只有验证通过并保存为可用版本的工作流才能被问答页选择。

前端主要文件为 `frontend/src/pages/ChatPage.tsx`、`frontend/src/pages/WorkflowEditorPage.tsx`、`frontend/src/components/chat/*`、`frontend/src/components/workflow/*`、`frontend/src/workflow/{nodeTypes,portTypes,validation}.ts` 和 `frontend/src/api/*`。旧 API 的兼容调用和新 Workflow API 的调用统一封装在 `frontend/src/api/client.ts`。

## 前端与 Workflow 验收

- 用户能在 `/chat` 提问并看到回答、引用、拒答状态和运行工作流版本。
- 用户能切换到 `/workflows/:workflowId`，拖拽、删除、配置和连接节点。
- 编辑器能阻止非法端口连接，并定位环路、断线和非法配置。
- 修改后的工作流能验证、保存为新版本并运行，运行轨迹能定位失败节点。
- 新增或替换 Node 不需要修改 Engine 核心。
- 前后端所有接口继续使用 `{success, data, error}`。
- 后端测试覆盖 Node Registry、DAG 校验、执行顺序、输入汇合、失败和跳过、证据门控、引用校验及兼容 API。
- 前端测试覆盖页面切换、节点拖拽、连线校验、配置表单、运行状态和回答引用展示；整体覆盖率目标高于 80%。

## 测试与验收

- 测试先行：针对解析与元数据、路由与三种分块的内容保全、embedding 校验、幂等上传与更新、RRF 融合去重、重排及阈值、无证据不调用生成器、非法引用拒绝输出、API 错误结构编写单元与接口测试。
- 优先验证端到端 API 流程（用可控的向量库和模型替身），再测各组件细节；目标覆盖率高于 80%。
- 可选真实集成测试由环境开关启用，对 Docker Milvus 验证集合及两类索引构建、中文 BM25 检索、稠密检索、替换和元数据；真实生成 API 的烟测需用户自行提供密钥，默认测试不花费外部费用。
- 文档提供 Windows/PowerShell 下的环境准备、Docker 启动、模型下载、启动 FastAPI、示例请求、测试与构建／检查命令。只有这些命令实际执行通过后才能在交付说明中声称成功。

## 后续扩展

输入解析器预留 URL 和图片/OCR 接口，鉴权、多租户、在线抓取、患者隐私数据与临床决策支持均不属于本次交付；引入这些能力需单独设计安全和数据边界。

## 选型依据（核对日期：2026-09-26）

- [Milvus HNSW_PQ](https://milvus.io/docs/hnsw-pq.md)、[BM25 全文检索](https://milvus.io/docs/full-text-search.md)、[中文分析器](https://milvus.io/docs/chinese-analyzer.md)
- [BGE-small-zh 模型资料](https://huggingface.co/BAAI/bge-small-zh)、[BGE 重排模型](https://huggingface.co/BAAI/bge-reranker-v2-m3)
- [DeepSeek 模型更新](https://api-docs.deepseek.com/updates/)、[Qwen Flash 型号](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)
