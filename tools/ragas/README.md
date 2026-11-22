# 独立 Ragas 评测环境

Ragas 是可选评测依赖，普通应用、测试和 CI 不需要安装。它与项目共用 `.env` 中的
`QWEN_API_KEY`、`QWEN_BASE_URL`、`QWEN_MODEL`，不需要新增密钥。

## 推荐：项目可选 extra

```powershell
uv pip install -e ".[dev,ragas]"
```

## 备选：完全隔离的虚拟环境

如果项目环境的 `openai<2` 与 Ragas 的传递依赖解析冲突，使用独立环境，避免影响主应用：

```powershell
uv venv .venv-ragas --python 3.11
uv pip install --python .venv-ragas\Scripts\python.exe -e . -r tools\ragas\requirements.txt
uv pip install --python .venv-ragas\Scripts\python.exe pytest pytest-asyncio
```

然后只用该环境运行 Ragas：

```powershell
.\.venv-ragas\Scripts\python.exe -m medical_rag.ragas_evaluate `
  --dataset outputs\evaluations\medical-health-v2\questions-runtime.jsonl `
  --report outputs\evaluations\v10-eval\report-medical-answer-v10.json `
  --output outputs\evaluations\v10-eval\report-ragas-smoke.json `
  --allow-paid-model --limit 2
```

普通环境检测不到 Ragas 时，CLI 会返回 2 并提示安装 `.[ragas]` 或使用本环境。

## 口径

- 语义指标（faithfulness、context_precision、context_recall、context_relevance）使用与最终生成
  prompt 一致的父窗口去重上下文。
- ID 精确指标（id_based_context_precision/recall）使用全部命中的子块 ID。
- MRR、NDCG、MAP、Hit@k、Core-Recall、parent-window、post-RAG cited、拒答/对话指标仍由本地平台计算。
- 单指标失败记录为 `null` 和固定错误码，不写成 0；报告不保存 prompt、答案正文或 API Key。
