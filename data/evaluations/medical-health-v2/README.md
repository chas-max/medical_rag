# 公开医疗健康知识评测集 v2.0（150 题分级多证据与安全拒答增强版）

本目录固定 **150 道** 中文医疗健康评测题（`120` 道可回答题 + `30` 道证据不足/安全拒答题），覆盖 `data/` 目录下全部 **35 份** 公开权威医疗指南、公卫规范、健康素养与食养指南。它面向本项目语料的检索排序质量（含分级相关性）、多证据聚合忠实度与医疗安全拒答能力评测，不代表临床诊疗能力认证。

## 相比 v1.0（500 题）的核心优化

1. **全量语料覆盖**：从 v1 的 7 份语料扩展至 `data/` 下全部 **35 份** 文件（涵盖慢阻肺、流感、儿童支原体肺炎、四大恶性肿瘤、精神障碍、慢性肾脏病、2024 版高血压/糖尿病/肥胖临床指南以及 2023–2026 版全套食养指南正文与问答）。
2. **高密度分级多证据标注**：每道可回答题标注 **3–6+ 条** 去重 `source_refs` 并显式区分 `relevance` 等级（`2=core` 核心必中证据、`1=supplementary` 等价/补充证据），避免单正例导致的 `Precision@k` 上限锁死、`Recall@k` 二值跳变及等价段落误判。
3. **医疗安全与拒答增强**：将 `insufficient_evidence` 比例提升至 **20%（30/150）**，消除模板化前缀并分散于 `scenario/procedure/numeric/comparison` 题型中；部分题目配备 `relevance=0` 近邻干扰锚点，用于计算检索命中相似片段时的 `decoy_abstention_rate`。
4. **增强检索指标**：除 `Precision@k / Recall@k / F1 / MAP@k / MRR@k` 外，新增基于 `chunk_grades` 的 **`NDCG@k`**、**`Core-Recall@k`** 与 **`Hit@k`**。

## 离线校验与入库重映射

在仓库根目录运行：

```powershell
python data/evaluations/medical-health-v2/tools/validate_dataset.py --dataset data/evaluations/medical-health-v2/questions.jsonl --manifest data/evaluations/medical-health-v2/manifest.json --registry data/evaluations/medical-health-v2/source-registry.json
```

入库全部 35 份语料并导出实际索引块清单 `chunks.json` 后，执行重映射（生成 `relevant_chunk_ids`、`chunk_grades` 及拒答题干扰 `context_chunk_ids`）：

```powershell
python data/evaluations/medical-health-v2/tools/remap_chunk_ids.py --dataset data/evaluations/medical-health-v2/questions.jsonl --chunks .\chunks.json --registry data/evaluations/medical-health-v2/source-registry.json --output .\medical-health-v2-runtime.jsonl
```

## 运行评估与三平台裁判汇总

```powershell
python -m medical_rag.evaluate --dataset .\medical-health-v2-runtime.jsonl --manifest data/evaluations/medical-health-v2/manifest.json --output .\rag-report-v2.json --top-k 5 --answer-provider qwen --judge-provider qwen
python data/evaluations/medical-health-v2/tools/export_for_judge.py --report .\rag-report-v2.json --dataset data/evaluations/medical-health-v2/questions.jsonl --output .\judge-input-v2.jsonl
python data/evaluations/medical-health-v2/tools/aggregate_judges.py --dataset data/evaluations/medical-health-v2/questions.jsonl --judge-input .\judge-input-v2.jsonl --chatgpt .\chatgpt-judged.jsonl --claude .\claude-judged.jsonl --qoder .\qoder-judged.jsonl --output .\judge-summary-v2.json
```
