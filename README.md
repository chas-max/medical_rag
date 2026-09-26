# 公开医疗资料 RAG 问答系统

FastAPI 后端：Markdown/TXT/可提取文字 PDF → 自动分块 → BGE 中文向量 → Milvus HNSW_PQ + BM25 → RRF 融合和 BGE 重排 → DeepSeek/Qwen Flash 的逐条引证问答。没有独立前端，可通过 `/docs` 演示。

> 仅用于公开资料检索与学习，不输入患者隐私，不替代医生诊断和治疗。引用 ID 与原文摘录的程序校验不能证明生成结论的医学正确性；关键结论应回看原资料。

## 本地准备（PowerShell）

需要 Python 3.11+、Docker Desktop（建议至少 8 GB 可用内存）和联网下载模型的能力。首次向量化和重排会下载 `BAAI/bge-small-zh-v1.5`、`BAAI/bge-reranker-v2-m3`；后续使用本地缓存。启动 Milvus 的 Compose 配置参考[官方 2.6.x 安装说明](https://milvus.io/docs/v2.6.x/install_standalone-docker-compose.md)。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
Copy-Item .env.example .env
docker compose up -d
docker compose ps
uvicorn medical_rag.api:app --app-dir src --reload
```

在 `.env` 中分别填写 `DEEPSEEK_API_KEY` / `QWEN_API_KEY`，只用某一家时另一项可空；不要提交 `.env`。默认模型 ID 为 `deepseek-flash`、`qwen3.8-flash`，可在环境变量中更换。`MILVUS_URI` 默认 `http://localhost:19530`。先等待 `standalone` 健康后再启动后端。当前只支持文字 PDF；扫描版会返回 422，不做 OCR。

访问 `http://127.0.0.1:8000/docs`。示例请求（另开一个 PowerShell）：

```powershell
curl.exe -F "file=@guide.md" http://127.0.0.1:8000/documents
curl.exe http://127.0.0.1:8000/documents
curl.exe -H "Content-Type: application/json" -d '{"question":"高血压如何随访？","top_k":5}' http://127.0.0.1:8000/search
curl.exe -H "Content-Type: application/json" -d '{"question":"高血压如何随访？","provider":"deepseek","top_k":5}' http://127.0.0.1:8000/answers
curl.exe http://127.0.0.1:8000/health
```

`POST /documents` 还接受可选 `source_id` 字段。同一来源与相同内容重复上传返回 `duplicate=true`；同一来源不同内容替换旧块。问答返回 `answer`、`citations` 和 `abstained`，每条结论带检索块 ID。所有接口统一 `{success,data,error}`。问题无相关证据时不调用外部模型，返回资料不足。

## 检查与测试

```powershell
python -m pytest -q --cov=medical_rag --cov-report=term-missing
python -m compileall -q src
docker compose config
```

真实 Milvus 集成测试默认跳过；在服务健康后单独启用：

```powershell
$env:RUN_MILVUS_INTEGRATION = '1'
python -m pytest tests/test_milvus_integration.py -q
Remove-Item Env:RUN_MILVUS_INTEGRATION
```

集成测试只创建、删除以 `rag_it_` 开头的临时集合；不会删除业务集合。默认测试不调用收费的生成 API。由于 PQ 对数据量有要求，少量样本在真实 Milvus 上可能无法训练索引；服务会报告错误，不静默更换索引。`HNSW_PQ` 参数为 `M=16`、`efConstruction=200`、`m=64`、`nbits=4`，默认检索融合 RRF `k=60`、重排阈值 0.5；阈值需用业务评测集校准。

## 范围与扩展

解析器、向量化器、存储、检索器、生成器相互独立；URL、图片/OCR 和展示前端作为后续任务。当前没有鉴权和多租户隔离，不要直接暴露到公网。实际部署前需要加入鉴权、限流、文件安全扫描、指标监控与备份策略。
