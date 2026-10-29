# 公开医疗资料 RAG 问答系统

面向公开医疗资料的中文检索问答项目。文档经父子分块和向量化后写入 Milvus；问答使用原问题向量检索、BM25 检索与 HyDE 向量检索，RRF 融合、重排、证据门控、回答规划和引用校验。React 提供文档上传、流式问答与来源查看。

本项目用于学习和公开资料检索，**不能替代医生诊断或治疗，不要输入患者隐私**。引用块 ID 校验只能确认引用指向检索到的资料，不能保证医学结论正确；关键结论应核查原文。

## 检索与安全

HyDE 由模型生成一段未经验证的假设性回答，仅用于向量检索；它不是证据，不会直接传给回答生成或引用校验。原问题始终参与向量和 BM25 检索，并用于重排与生成。HyDE 模型请求默认限时 10 秒，可通过 `HYDE_TIMEOUT_SECONDS` 调整；请求失败或输出无效时该支路返回空结果，正常使用原问题的两路检索，运行记录标注降级。设置 `HYDE_ENABLED=false` 后重启服务可关闭该支路，用于同口径评测基线。HyDE 可能增加模型费用和延迟，其收益需要实验验证，不能假设必然提升。

本地重排不可用时默认拒答；证据不足时不输出未锚定的医疗结论。可选联网检索未默认启用。系统目前无用户鉴权，勿将其直接暴露到公网。

## 本机启动（PowerShell）

需要 Python 3.11+、Node.js 20.19+/22.12+、Docker Desktop 和本地 BGE 模型缓存。首次准备嵌入与重排模型可能需要下载模型权重。新项目的 `.env` 已单独保存复制的模型配置和独立服务配置，包含明文密钥；请勿提交或分享该文件。

```powershell
Set-Location D:\medical-rag-resume
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
docker compose up -d
npm --prefix frontend install
npm --prefix frontend run build
uvicorn medical_rag.api:app --app-dir src --host 127.0.0.1 --port 8001
```

在三个单独终端、同一项目目录及其虚拟环境中启动后台进程：

```powershell
python -m medical_rag.worker relay
python -m medical_rag.worker ingest
python -m medical_rag.worker memory
```

访问 `http://127.0.0.1:8001/ui/chat`；API 文档在 `http://127.0.0.1:8001/docs`。上传后等待入库任务成功，再开始检索。环境独立使用 Milvus 19531、RabbitMQ 5673、管理界面 15673、集合 `medical_rag_resume_chunks` 和本项目目录下的会话数据；这些服务的数据与原项目不共享。启动前请检查端口是否空闲。已复制的模型 API Key 仍属于同一上游账户，会产生该账户费用；不要在日志或截图中暴露它。

若使用 Vite 开发服务器，可在 `frontend/vite.config.ts` 中将代理目标设为本项目后端端口。独立环境需要重新入库资料；不要把新项目配置改为原项目的集合或消息队列，除非明确接受共享数据的风险。

项目优先读取进程环境变量，然后读取本项目根目录的 `.env`。如果终端中已经设置了 `MILVUS_URI`、`COLLECTION_NAME` 或 `RABBITMQ_URL` 等配置，它们会覆盖独立项目的配置；启动前请检查并清除不希望继承的变量，避免意外连接原项目的服务。

## 测试与评测

```powershell
python -m pytest -q -m 'not integration' --deselect tests/test_medical_health_release_integration.py::test_pinned_sources_remap_all_500_questions_with_default_chunking
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run build
```

普通自动化测试应使用替身依赖，不应产生真实模型费用。真实模型、Milvus 和 RabbitMQ 的端到端测试需要单独准备环境，且可能产生费用。对比 HyDE 前后，应使用相同语料、索引、题集、模型和其他参数，分别记录 Recall@k、MRR、答案质量、证据不足时的拒答率、延迟及模型成本；尚未进行同口径比较，不能宣称 HyDE 已带来特定提升。

**当前验证状态：** 上述后端命令得到 962 项通过、4 项跳过、2 项排除；其中一项被排除的 500 题评测集原文锚点映射测试在原项目与本项目均失败，完整测试套件尚未全部通过。修复或重新核对锚点前，不能将该题集作为可用的完整评测结果。独立项目的真实模型与外部服务端到端测试尚未执行。

`data/evaluations/medical-health-v1` 包含评测资料与工具；评测块 ID 需要按当前独立索引重新映射，模型裁判分不能替代医学专家判断。部分源资料可能有独立的版权和使用限制，发布前需核对。
