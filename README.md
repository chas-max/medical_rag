# 公开医疗资料 RAG 问答系统

双页面医疗资料检索系统：Markdown/TXT/可提取文字 PDF → 自动分块 → BGE 中文向量 → Milvus HNSW_PQ + BM25 → RRF 融合和 BGE 重排 → Qwen Flash 的逐条引证问答，检索不足时可由生成阶段联网补充证据。FastAPI 后端以可注册 Node 和 DAG Workflow Engine 编排流程；React 前端提供问答与拖拽式工作流编辑器。

> 仅用于公开资料检索与学习，不输入患者隐私，不替代医生诊断和治疗。引用 ID 与原文摘录的程序校验不能证明生成结论的医学正确性；关键结论应回看原资料。

## 本地准备（PowerShell）

需要 Python 3.11+、Node.js 20.19+/22.12+、Docker Desktop（建议至少 8 GB 可用内存）和联网下载模型的能力。首次向量化和重排会下载 `BAAI/bge-small-zh-v1.5`、`BAAI/bge-reranker-v2-m3`；后续使用本地缓存。权重加载固定 `local_files_only=True`，缓存缺失时重排只留一行 warning 就退化成词面分数——此时检索指标反映的是 RRF 名次而非重排能力，跨批次不可比；huggingface.co 不可达时先用 `HF_ENDPOINT=https://hf-mirror.com` 把权重放进缓存。启动 Milvus 的 Compose 配置参考[官方 2.6.x 安装说明](https://milvus.io/docs/v2.6.x/install_standalone-docker-compose.md)。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
if (-not (Test-Path -LiteralPath .env)) { New-Item -ItemType File -Path .env | Out-Null }
```

已有 `.env` 会原样保留，不会覆盖。逐条引证问答使用 Qwen Flash；在 `.env` 中设置 `QWEN_API_KEY`，默认模型 ID 为 `qwen3.8-flash`。不要提交 `.env`。`MILVUS_URI` 默认 `http://localhost:19530`。当前只支持文字 PDF；扫描版会返回 422，不做 OCR。联网补充检索默认关闭，需要时在 `.env` 设置 `TAVILY_API_KEY`（见“联网补充检索”）。配置好密钥后继续启动：

```powershell
docker compose up -d
docker compose ps
npm --prefix frontend install
npm --prefix frontend run build
uvicorn medical_rag.api:app --app-dir src --reload
```

同步生成请求显式设置 `enable_thinking=false`，避免 Qwen 默认思考模式在多段证据问答中超过等待时间；客户端超时上限为 120 秒。修改生成代码后，未启用 `--reload` 的后端需要重启才能生效。

启动后端前，先等待 `standalone` 健康。

Compose 默认通过 DaoCloud Docker Hub 镜像代理拉取 Milvus 和 MinIO，以适配 Docker Hub 不可达的网络；镜像地址可在 `.env` 中用 `MILVUS_IMAGE` / `MINIO_IMAGE` 覆盖。生产部署建议换成组织认可并固定摘要的镜像仓库。

访问 `http://127.0.0.1:8000/ui/chat` 使用问答页，或访问 `http://127.0.0.1:8000/ui/workflows/medical-answer` 编辑工作流；`http://127.0.0.1:8000/docs` 保留 API 文档。前端构建目录 `frontend/dist` 不提交；每次部署需先执行 `npm --prefix frontend run build`。开发模式可另开终端运行 `npm --prefix frontend run dev`，打开 Vite 提供的 `/ui/chat`；其 `/api` 代理转发到本机 8000 端口。

示例请求（另开一个 PowerShell）：

```powershell
curl.exe -F "file=@guide.md" http://127.0.0.1:8000/documents
curl.exe http://127.0.0.1:8000/documents
curl.exe -H "Content-Type: application/json" -d '{"question":"高血压如何随访？","top_k":5}' http://127.0.0.1:8000/search
curl.exe -H "Content-Type: application/json" -d '{"question":"高血压如何随访？","provider":"qwen","top_k":5}' http://127.0.0.1:8000/answers
curl.exe http://127.0.0.1:8000/health
```

`POST /documents` 还接受可选 `source_id` 字段。同一来源与相同内容重复上传返回 `duplicate=true`；同一来源不同内容替换旧块。问答返回 `answer`、`citations` 和 `abstained`，每条结论带检索块 ID。所有接口统一 `{success,data,error}`。问题无相关证据时不调用外部模型，返回资料不足。

## 工作流与前端

默认流程位于 `workflows/*.v*.json`：`medical-answer` 已发布两个版本，v2（当前生效）在证据门控和生成节点上开启了联网补充检索，并把引用校验的证据改接生成节点；v1 保留为不含联网的历史版本。`medical-answer-expanded` 走问题路由与扩展检索；`medical-search` 负责检索；`document-ingestion` 负责上传解析、分块、向量化和入库。现有 `/answers`、`/search`、`/documents` 经默认流程执行。`GET /nodes` 返回节点库的类型、分类、输入输出端口和配置 Schema；`GET /workflows` 只列已发布版本。编辑器可拖拽、连线、修改参数、复制或删除节点；校验后发布为不可覆盖的新版本，运行记录显示逐节点状态与耗时。草稿允许暂时不完整，但不能直接运行；问答页只使用已发布版本。工作流 JSON 不保存模型密钥；新增能力可注册新 Node 并连入工作流。

问答页支持会话切换、本地浏览器历史、资料上传和引用原文面板。问答页默认收起工作流和证据数高级设置；回答按要点展示，引用编号可打开来源原文，不显示运行 ID。会话只存当前浏览器的 `localStorage`，不是服务器账户数据；不要输入患者隐私资料。编辑器的“运行已发布版”执行的是后端当前发布版本，尚未发布的画布修改不会参与运行。当前无用户鉴权，工作流编辑 API 也不能直接暴露到公网。

工作流 API 为 `GET /nodes`、`GET/POST /workflows`、`GET/PUT /workflows/{id}`、`POST /workflows/{id}/validate`、`POST /workflows/{id}/runs` 和 `GET /runs/{run_id}`。例如：

```powershell
curl.exe -H "Content-Type: application/json" -d '{"inputs":{"question":"高血压如何随访？","provider":"qwen","top_k":5}}' http://127.0.0.1:8000/workflows/medical-answer/runs
```

`run_id` 的追踪记录目前保存在进程内，服务重启后不可查询；工作流定义保存在 `workflows/` 版本文件中，部署时需保持该目录可写并纳入备份。

## 联网补充检索

问答的生成阶段先自检检索质量，分三种情况决定是否触网：本地检索**没有命中**时用 Tavily 检索公开网页资料兜底生成；检索**内容模糊**（最高重排分低于 `CLEAR_EVIDENCE_THRESHOLD`，或证据少于 `MIN_CLEAR_EVIDENCE` 条）时先用原问题检索、再由模型改写检索词补充，网页内容与本地证据块一同进入提示词；检索**已经清晰**时直接依据本地资料作答，不提供联网工具。工具以可注册形式接入（`src/medical_rag/tools.py` 的 `Tool` 与 `ToolRegistry`），`tavily_search` 只暴露 `query` 参数，模型发起的工具轮次按 OpenAI function-calling 协议写回会话，最多 `MAX_AGENT_TOOL_CALLS` 轮。

网页命中转成带来源的证据块（块 ID 形如 `web-…`，标题在 `filename`、网址在 `section`，正文按 `WEB_CONTENT_MAX_CHARS` 截断，同一网址只保留一条），因此仍受逐字引用校验约束：`citations[].origin` 为 `web` 的结论来自公开网页、未经本地资料核实，可信度低于知识库引用，不替代医生诊断。`TAVILY_API_KEY` 留空时工具集为空，所有流程与不联网时完全一致，默认测试与离线评测不产生外部请求；Tavily 故障统一映射为 `WEB_SEARCH_*` 错误码且不外泄上游响应体，工具失败只作为错误负载回给模型，不中断本次回答。

联网补充默认已开启在已发布的 `medical-answer` v2 上，`/answers` 与问答页都会走它；自行连线时把 `evidence.gate` 的 `allow_web_search` 与 `generation.llm` 的 `web_search` 都设为 `true` 即可。`TAVILY_API_KEY` 留空时工具集为空，门控退回原来的过早拒答，回答路径与不联网完全一致。其余可调项：`TAVILY_BASE_URL`、`TAVILY_SEARCH_DEPTH`（`basic`／`advanced`）、`TAVILY_MAX_RESULTS`、`TAVILY_TIMEOUT_SECONDS`。默认开启后，只要检索未达到"清晰"标准就会实际产生 Tavily 计费请求；离线评测仍走不联网的 `generator.answer(...)`。

## 检查与测试

```powershell
python -m pytest -q --cov=medical_rag --cov-report=term-missing
python -m ruff check src tests
python -m compileall -q src
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
docker compose config
```

真实 Milvus 集成测试默认跳过；在服务健康后单独启用：

```powershell
$env:RUN_MILVUS_INTEGRATION = '1'
python -m pytest tests/test_milvus_integration.py -q
Remove-Item Env:RUN_MILVUS_INTEGRATION
```

集成测试只创建、删除以 `rag_it_` 开头的临时集合；不会删除业务集合。默认测试不调用收费的生成 API。Milvus 对小于默认每段 1,024 行的语料可能以暴力扫描检索，即使集合已配置 `HNSW_PQ`；因此物理索引是否建成须查看 `describe_index().indexed_rows`，可选集成测试使用 2,048 行并检查此指标。[Milvus 性能 FAQ](https://milvus.io/docs/v2.6.x/performance_faq.md)。配置不兼容会明确报错，但小语料的内部扫描不等于切换了集合的索引类型。`HNSW_PQ` 参数为 `M=16`、`efConstruction=200`、`m=64`、`nbits=4`，默认检索融合 RRF `k=60`、重排阈值 0.5；阈值需用业务评测集校准。重排只打分给 RRF 头部 `rerank_k=15` 个候选（`top_k` 更大时自动放宽池宽）：480 题里标注块的 RRF 最好名次中位数为 0，池宽 15 会让 1.9%（9/477）的题在重排前就丢掉标注块，12 为 3.8%。`bge-reranker-v2-m3` 声明 8192 token 窗口，CPU 上给 35 候选打整块要 36 秒，因此固定 `max_length=512` 与 `torch_dtype=bfloat16`（15 候选约 9.3 秒，fp32 约 16 秒且 2.3GB 权重在 16GB 机器上会被换页）；实测 bf16 相对 fp32 在 15 候选全序上零逆序、分数最大偏差 0.0024。

## 离线评估

项目另有固定的[公开医疗健康知识 500 题评测集](data/evaluations/medical-health-v1/README.md)，覆盖七份公开语料，包含原文锚点、精确配额、20 道证据不足题和跨 ChatGPT／Claude Code／Qoder 的统一裁判协议。先用目录中的校验工具确认来源哈希，再以实际入库块清单映射生成 runtime 题集，最后显式传入 `--manifest` 运行评估；不要直接用未映射的源题集执行计分。原文锚点与块 ID 分离，重建索引后必须重新映射。题库答案分须待外部三平台裁判各自评分后汇总；本地三项语义评分不能冒充最终答案分。

先入库公开资料，从 `/search` 返回的 `data[].chunk.id` 找到候选块，人工核对原文并标注每个问题的**全部已知相关块**。UTF-8 JSONL 文件每行一题；`id` 可省略，`relevant_chunk_ids` 必须是非空、不重复的块 ID 数组：

```json
{"id":"followup-1","question":"高血压患者如何随访？","relevant_chunk_ids":["人工核对后的真实块ID"]}
```

把示例 ID 换成入库后的实际 ID。更新或替换文档后块 ID 可能变化，须重新核对题集。确认 Milvus 已启动，且 `.env` 中配置了 `QWEN_API_KEY`，再显式运行：

```powershell
python -m medical_rag.evaluate --dataset .\my-eval.jsonl --output .\my-report.json --top-k 5 --answer-provider qwen --judge-provider qwen
```

命令只在完整校验题集后初始化模型和 Milvus；模型请求逐题顺序执行。答案生成和评审均使用 Qwen，但仍会分别发起模型请求，可能分别计费，也可能存在同模型自评偏差。无需新 HTTP 接口。结果文件存在时会覆盖；不得让 `--output` 与题集同路径。建议把题集和报告放在仓库外，报告包含原问题、检索全文、模型回答和评分理由，不要放入患者隐私信息，也不要提交密钥或报告。

报告 JSON 的 `cases` 保留每题检索块、命中块、答案、每块上下文相关度、三项模型评审分数和安全错误码；`summary` 包含宏平均 `precision`、`recall`、`f1`、`map`、`mrr`、三项语义指标、有效题数、拒答率及评审失败数。各检索指标均基于现有门槛过滤后的最终有序结果：Precision@k = 命中数 / 实际返回数（空结果为 0），Recall@k = 命中数 / 标注相关块总数，F1 为两者调和平均；AP@k = 每个命中名次的 Precision 之和 / `min(标注相关块数,k)`，MAP@k 为逐题 AP 的平均；RR@k = 1 / 首个命中名次，MRR@k 为逐题 RR 的平均。排名只考虑前 k 个唯一块。若标注不完整，Recall、MAP 等数字会误导。

faithfulness（忠实度）评回答是否受到本次证据支持；上下文相关度先对每条返回证据评分再逐题平均；答案相关性评回答是否回应问题。三者均由选定模型给出 0–1 分及理由，不能证明医学事实正确。拒答时三项均为 `null`；评审出错仅影响对应维度，错误不冒充 0 分，汇总只平均有效分。单题检索／生成失败也在报告中标记，不泄露外部服务的原始错误信息。默认测试不用付费 API；真实模型评分与真实 Milvus 仍需在你的环境自行验证。

## 范围与扩展

解析器、向量化器、存储、检索器、生成器均可作为独立 Node 组合；URL 摄取、图片/OCR 和患者隐私资料不在当前范围（联网检索只作为生成阶段的补充证据，不落库）。当前没有鉴权和多租户隔离，不要直接暴露到公网。实际部署前需要加入鉴权、限流、文件安全扫描、指标监控与备份策略。
