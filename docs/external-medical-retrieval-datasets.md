# 小型医疗评测集调研：可用于「建索引 / 测试问题 / 标准相关块」三件套的外部数据集

> 调研时间：2026-11-19　分支：`feat/eval-dataset-survey`
> 目的：为 `medical-health-v2`（150 题、中文指南语料）之外补充**可外部复现**的检索评测集，
> 要求同时具备 ①建索引用语料（corpus）②测试问题（queries）③标准相关块答案（gold chunks / qrels）。
>
> 本文所有"实测"数字均由本仓库脚本实测（`outputs/dataset-survey/*.py`，该目录被 `.gitignore` 忽略，
> 可随时重跑）；"卡片"数字来自数据集官方 Dataset Card / README，已在正文标注。

---

## 0. 结论速览

| 结论 | 内容 |
|---|---|
| **唯一中文三件套** | **CMIRB**（AutoMIR，arXiv 2410.20050）：10 个子集，均为 BEIR 布局 `corpus.jsonl` / `queries.jsonl` / `qrels/test.jsonl`，Apache-2.0，单子集 7–22 MB 即可落地 |
| **最适合"多正例 + 分级"** | **BEIR NFCorpus**：3,633 篇摘要 / 323 道判定题 / 12,334 条 qrels（1、2 两级），实测平均 **38.2 条正例/题**，67% 的题 ≥8 条正例 |
| **最接近本项目 0/1/2 分级** | **Yale medical-rag-expert-evaluation-200**：200 题 × 16 候选段 × `full/partial/none` 三级人工判定；但**仅 200 题且无完整语料**（只有 top-16 候选），CC BY-NC 4.0 **不可商用** |
| **不满足三件套（只有问答对）** | PubMedQA、K-QA、MedQuAD、MIRAGE：有"问题+答案/要点"，但**没有可索引语料 + 相关块标注**，不能直接算 Recall@k |
| **最大风险** | CMIRB 每个查询**只有 1 条 gold**，与本项目 `medical-health-v1` 踩过的坑（单正例锁死 `Precision@k` 上限）完全一致，只能用于"召回位置"类指标，不能用于 P/R 双高评估 |

**推荐组合（按优先级）**

1. **CMIRB/`DXYDiseaseRetrieval` + `DXYConsultRetrieval`** —— 中文、体积小、gold 可 100% 解析回原文，作为"中文召回通道"的外部对照集。
2. **BEIR NFCorpus** —— 英文、多正例、两级相关，作为"多块召回 + 分级 nDCG"的能力对照集。
3. 若要"分级相关性"直接对齐本项目的 `relevance=2/1/0`，只考虑 **Yale-200**，但必须接受非商用许可与 200 题规模。

---

## 1. 筛选口径

本项目 `data/evaluations/medical-health-v2/` 的契约是「每道可回答题 3–6+ 条 `source_refs`（含 `relevance` 2/1）
→ 重映射为 `relevant_chunk_ids` / `chunk_grades`」，而评测内核（`src/medical_rag/evaluation_dataset.py`）实际只强依赖：

- 必填：`id`、`question`、`relevant_chunk_ids`
- 富题字段（可选整组）：`reference_answer`、`key_points`、`answerability`、`category`、
  `question_type`、`difficulty`、`source_refs`

因此外部数据集只要能提供 **「问题 → 若干标准相关块 ID」**，就能以 `{"id","question","relevant_chunk_ids"}`
的 legacy 三字段行直接进 `evaluate.py`。据此把候选集分成三档：

| 档位 | 含义 | 能否直接评测 |
|---|---|---|
| **A. 三件套齐** | 语料 + 问题 + 相关块标注（chunk/passage 级） | 能，转 JSONL 即可 |
| **B. 半套** | 有问题 + 答案/要点，但无语料或无块级标注 | 不能直接算 Recall@k，只能做答案质量/忠实度评测 |
| **C. 需自建** | 只给 PMID / URL | 需自建索引后再人工标注 |

---

## 2. 候选总表（实测数字）

> 除注明"卡片"外，均实测。`正例/题` 只统计**有 qrels 的查询**（BEIR 的 `queries.jsonl` 里混有大量无标注查询）。

| 数据集 | 语言 | 语料规模 | 测试问题 | 标准相关块 | 正例/题 | 许可 | 档位 |
|---|---|---:|---:|---|---:|---|---|
| **CMIRB / DXYDiseaseRetrieval** | 中 | 54,021 段（均 191 字符） | 1,255 | 1255 条 `q_id→p_id` | 1.00 | Apache-2.0 | **A** |
| **CMIRB / DXYConsultRetrieval** | 中 | 12,577 段（均 370 字符） | 943 | 943 | 1.00 | Apache-2.0 | **A** |
| **CMIRB / MedExamRetrieval** | 中 | 27,871 段（均 494 字符） | 697 | 697 | 1.00 | Apache-2.0 | **A** |
| CMIRB / DuBaikeRetrieval | 中 | 56,441 | 318 | 318 | 1.00 | Apache-2.0 | A |
| CMIRB / IIYiPostRetrieval | 中 | 27,570 | 789 | 789 | 1.00 | Apache-2.0 | A |
| CMIRB / CSLCiteRetrieval | 中 | 36,703 | 573 | 573 | 1.00 | Apache-2.0 | A |
| CMIRB / CSLRelatedRetrieval | 中 | 36,758 | 439 | 439 | 1.00 | Apache-2.0 | A |
| CMIRB / CovidRetrieval | 中 | 100,001 | 949 | 959 行 | 1.01 | 卡片未标 | A |
| C-MTEB / MedicalRetrieval | 中 | 100,999 | 1,000 | 1,000 | 1.00 | 卡片未标 | A |
| C-MTEB / CmedqaRetrieval | 中 | 100,001 | 3,999 | 7,449 行 | **1.86**（最多 19） | 卡片未标 | A |
| **BEIR / NFCorpus** | 英 | 3,633 篇（均 1,590 字符） | 323（另有 324 dev） | 12,334 行，1/2 两级 | **38.19**（中位 16） | CC BY-SA 4.0 | **A** |
| **BEIR / SciFact** | 英 | 5,183 篇（均 1,498） | 300 | 339 行，全 1 级 | 1.13（最多 5） | CC BY-SA 4.0 | **A** |
| BEIR / ViHealthQA | 越 | 9,896 段（均 496） | 2,013（另有 993 dev） | 5,019 行 | 2.49（dev 恒 3） | 卡片未标 | A |
| BEIR / TREC-COVID | 英 | 171,332 篇 | 50 | 66,336 行（-1/0/1/2） | 493/题 | CC BY-SA 4.0 | A（语料过大） |
| **Yale medical-rag-expert-200** | 英 | 3,200 候选段（均 1,134 字符） | 200 | 30,784 条 `gold_statement × passage`（full 529 / partial 1,092 / none 29,163） | **3.48**（中位 2） | **CC BY-NC 4.0** | A（无完整语料） |
| PubMedQA `pqa_labeled` | 英 | 3,358 段摘要（题内自带） | 1,000 | gold 摘要 3.36 篇/题 | — | MIT | **B** |
| MIRAGE | 英 | 无（需外部 MedRAG 语料） | 7,663 | 无（仅 1,118 题带 PMID） | — | 卡片 PD/NLM | **C** |
| K-QA | 英 | 无（仅 source URL） | 201 | 892 条 must-have 陈述 | — | MIT | **B** |
| MedQuAD | 英 | 47,441 问答对 | — | 无块级标注 | — | 卡片未标 | **B** |
| BioASQ Task B | 英 | 需注册下载 | 4,719（11b 训练集） | relevant documents + snippets | — | NLM | B/C |

---

## 3. 重点候选详解

### 3.1 CMIRB（中文，唯一可用的中文三件套）

来源：AutoMIR 论文（[arXiv:2410.20050](https://arxiv.org/abs/2410.20050)），HF 组织 [`CMIRB`](https://huggingface.co/CMIRB)，
覆盖 5 类任务、10 个子集，全部采用 BEIR 目录布局，**Apache-2.0**（`DXYDiseaseRetrieval` /
`DXYConsultRetrieval` / `MedExamRetrieval` / `DuBaikeRetrieval` / `IIYiPostRetrieval` /
`CSLCiteRetrieval` / `CSLRelatedRetrieval` 的卡片均标 Apache-2.0；`CovidRetrieval`、
`C-MTEB/*` 系列卡片未标许可，商用前需人工确认）。

**实测结构**（以 `DXYDiseaseRetrieval` 为例，已下载校验全部 1,255 条 qrels）：

```
Q[q0000] 微小病变型肾病使用糖皮质激素进行治疗，需要注意哪些不良反应？
  gold[p38322] 治疗过程中需要注意糖皮质激素常见的不良反应：皮肤变薄和紫癜、类库欣表现和体重增加、
               睡眠紊乱、血糖增高、白内障、眼压增高、心血管疾病风险增加、血压升高……
```

- **gold 可解析率 100%**：`qrels/test.jsonl` 里的 `p_id` 全部能在 `corpus.jsonl` 中命中
  （`DXYDiseaseRetrieval` 54,021 段、`DXYConsultRetrieval` 12,577 段，均为 0 缺失）。
- **gold 内容确实是"答案块"**：上面这条 p38322 直接列出激素不良反应清单，不是泛泛的相邻段落。
- **查询是真实患者/医生口吻**：`DXYConsultRetrieval` 平均 170 字符（"症状及患病时长：10 天……需要解答的问题：……"），
  `DXYDiseaseRetrieval` 平均 24 字符（`微小病变型肾病使用糖皮质激素……？`），`MedExamRetrieval` 是单选题改写成问句。

**关键缺陷（必须写进评测口径）**：**每个查询只有 1 条正例**（8 个子集实测 mean=1.00，`CovidRetrieval` 为 1.01，
`CmedqaRetrieval` 是唯一平均 1.86 的例外，最多 19 条）。这与 `docs/diagnosis-retrieval-recall.md` §1.2 记录的 v1 问题同构：
`Precision@k ≤ 1/k`（k=8 时上限 0.125），**不能用来复现"P/R 双高"评估**，只适合看
`Recall@k / MRR / Hit@k` 与"gold 落在第几名"。

**落地体积**（parquet 实测）：`DXYConsult` 语料 7.6 MB、`DXYDisease` 16.1 MB、`MedExam` 22.0 MB，
`C-MTEB/MedicalRetrieval` 25.0 MB、`CmedqaRetrieval` 60.8 MB。单子集完全可以在本机跑全量。

### 3.2 BEIR NFCorpus（英文，多正例 + 分级的最佳样本）

[`BeIR/nfcorpus`](https://huggingface.co/datasets/BeIR/nfcorpus) + [`BeIR/nfcorpus-qrels`](https://huggingface.co/datasets/BeIR/nfcorpus-qrels)，
**CC BY-SA 4.0**，压缩包仅 **2.4 MB**（[官方 zip](https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip)）。

实测：

| 项 | dev | test |
|---|---:|---:|
| 有标注查询 | 324 | 323 |
| qrels 行 | 11,385 | 12,334 |
| 分级分布 | 1 级 10,864 / 2 级 521 | 1 级 11,758 / 2 级 576 |
| 正例/题 | 35.14（中位 18.5，max 919） | **38.19（中位 16，max 475）** |
| ≥8 条正例的题占比 | 71% | 67% |
| 含 2 级正例的题 | 115 | 119 |

- 文档 = 营养/医学摘要（title + text），**天然就是"块"**（均 1,590 字符），可 1 篇 = 1 chunk 直接建索引。
- `queries.jsonl` 有 3,237 条，但**只有 647 条有 qrels**（test 323 + dev 324）；按 BEIR 惯例应先用 qrels 的 `query-id` 过滤，
  否则 Recall 会被无标注查询稀释（这是接 BEIR 最常见的坑）。
- 与项目最贴的地方：**多正例 + 1/2 两级**，正好能验证 `chunk_grades`、`Core-Recall`、`nDCG` 这套分级指标是否工作。

### 3.3 BEIR SciFact（英文，证据句级）

**5,183 篇摘要**、300 道有标注 claim、339 条 qrels（全 1 级，**1.13 正例/题**）。
`scifact` 原版还带 `evidence` 句级标注，但 BEIR 版只给文档级。适合"单正例 + 二值"的对照组，
不适合分级。压缩包 **2.8 MB**。

### 3.4 Yale medical-rag-expert-evaluation-200（最接近本项目分级口径，但非商用）

[`Yale-BIDS-Chen/medical-rag-expert-evaluation-200`](https://huggingface.co/datasets/Yale-BIDS-Chen/medical-rag-expert-evaluation-200)
（论文 [arXiv:2511.06738](https://arxiv.org/abs/2511.06738)），**CC BY-NC 4.0（仅非商用）**，全部 parquet 合计仅 **2.4 MB**。

实测 6 张表：`responses` 800 / `passages` 3,200 / `retrieval` 30,784 / `selection` 26,032 /
`factuality` 15,970 / `completeness` 7,696。

- 200 题（100 来自 K-QA 真实患者问、100 来自 MedBullets 考试题），每题 **top-16 候选段**，段均 1,134 字符。
- 判定粒度是「`gold_statement` × `passage`」：`full` 529 / `partial` 1,092 / `none` 29,163（30,784 行）。
- 去重到 (query, passage) 后：**每题相关段 mean 3.48 / median 2 / max 16**；**62/200 题在 top-16 内一条相关段都没有**。
- 3,200 段里 **2,905 段文本唯一**（268 条 citation 被多题复用），所以"语料"只有 ~2.9k 段，
  **不是完整知识库**，不能用于"从全量指南里召回"，只能用于**重排 / 证据筛选**评估。
- 与项目对齐度最高：`full→relevance 2`、`partial→relevance 1`、`none→0`，且带
  `gold_statement`（可当 `key_points`）与 `gold_answer`（可当 `reference_answer`）。

### 3.5 只算"半套"的数据集（不要用来算 Recall@k）

| 数据集 | 有什么 | 缺什么 |
|---|---|---|
| **PubMedQA `pqa_labeled`**（MIT，1,000 题） | 题 + 3.36 篇 gold 摘要/题 + yes/no/maybe | 没有独立语料库：gold 摘要就写死在题里，无干扰项，无法算 Recall |
| **MIRAGE**（7,663 题） | 5 个多选题集（medqa 1,273 / medmcqa 4,183 / mmlu 1,089 / pubmedqa 500 / bioasq 618） | 只有选项与答案；1,118 题带 PMID，**无块级 gold，无语料**（MedRAG 的 textbooks 1 GB、pubmed 279 GB） |
| **K-QA**（MIT，201 题） | 题 + 892 条 must-have 陈述（4.4 条/题）+ 1 个 source URL | 无段落文本、无块 ID |
| **MedQuAD**（47,441 对） | 问答对 | 无块级相关性标注 |
| **BioASQ Task B** | relevant documents + snippets + exact/ideal answer（11b 共 4,719 题） | **官方需注册**（[participants-area](https://participants-area.bioasq.org/datasets/) 明确"仅注册用户可下载"），HF 上的 `BeIR/bioasq` 已 401 不可匿名获取 |

---

## 4. 接入本项目的字段映射

外部集 → `medical-health-v2` 契约的对照：

| 外部字段 | 本项目字段 | 说明 |
|---|---|---|
| `queries.jsonl.id` | `id` | 需加前缀避免与 `MHE2-*` 冲突，如 `cmirb-dxydisease-q0000` |
| `queries.jsonl.text` | `question` | 中文咨询体可直接用，但需注意长度（`DXYConsult` 均 170 字符） |
| `qrels.corpus-id`（`p_id`） | `relevant_chunk_ids` | 建索引时**必须用同一 `p_id` 作 chunk 主键**，否则重映射失败 |
| `qrels.score`（1/2） | `chunk_grades`（1/2） | 仅 BEIR NFCorpus 有 2 级；CMIRB 全为 1 |
| `passages.passage`（Yale） | `source_refs[].quote` | 可反查 `citation` 作 `source_id` |
| `passages.label` full/partial/none | `relevance` 2/1/0 | 一一对应 |
| `retrieval.gold_statement` | `key_points` | 陈述级要点，与项目 `key_points` 语义一致 |

**注意**：`relevance` 分级只存在于"富题"分支；若只给 `{"id","question","relevant_chunk_ids"}` 三字段，
`validate_evaluation_row` 会走 legacy 分支（`is_rich=False`），**`chunk_grades` 会被丢弃**，`nDCG/Core-Recall`
退化。想保留分级就必须补齐富题字段（`reference_answer`、`key_points`、`answerability`、`category`、
`question_type`、`difficulty`、`source_refs`）——这是接外部集的主要工作量。

---

## 5. 落地步骤（建议）

1. **落盘**：`hf download CMIRB/DXYDiseaseRetrieval --repo-type dataset --local-dir data/external/cmirb-dxydisease`
   （NFCorpus 用官方 zip，2.4 MB）。
2. **建独立集合**：`MILVUS` 里另建 `medical_cmirb_dxydisease` collection，**chunk 主键直接用 `p_id`**，
   文本用原始 `corpus.text`（不再二次切分，避免 gold ID 失配）。
3. **转 JSONL**：先产出 legacy 三字段版本跑通 `Recall@k / MRR / Hit@k`，再按第 4 节补富题字段跑分级指标。
4. **跑评测**：`python -m medical_rag.evaluate --dataset data/external/cmirb-dxydisease/runtime.jsonl --top-k 8 --output outputs/eval/cmirb-dxydisease.json`
   （`--manifest` 可省略；工作流版另加 `--workflow-id`）。
5. **口径声明**：报告里必须写"每题 1 条 gold"，否则 `Precision@k` 会被误读。

---

## 6. 复现命令

```powershell
# 1) BEIR NFCorpus / SciFact / ViHealthQA（官方 zip，秒级）
Invoke-WebRequest https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip -OutFile nfcorpus.zip
Expand-Archive nfcorpus.zip -DestinationPath beir-nfcorpus

# 2) CMIRB qrels/queries（jsonl，直连）
Invoke-WebRequest https://huggingface.co/datasets/CMIRB/DXYDiseaseRetrieval/resolve/main/qrels/test.jsonl -OutFile dxydisease-qrels.jsonl
Invoke-WebRequest https://huggingface.co/datasets/CMIRB/DXYDiseaseRetrieval/resolve/main/queries.jsonl -OutFile dxydisease-queries.jsonl

# 3) CMIRB / C-MTEB / Yale / PubMedQA 的 parquet（需 pyarrow，本仓库 .venv 未装）
uv venv .venv-survey
uv pip install --python .venv-survey\Scripts\python.exe pyarrow
# 再用 datasets-server 的 parquet 转换地址下载，例如：
# https://datasets-server.huggingface.co/parquet?dataset=BeIR/nfcorpus-qrels
```

实测脚本（均在 `outputs/dataset-survey/`，被 `.gitignore` 忽略）：

| 脚本 | 作用 |
|---|---|
| `stats.py` | CMIRB 三个子集的 qrels 统计（正例/题、查询长度、样例） |
| `stats_beir.py` | NFCorpus / SciFact 的 corpus/queries/qrels 全量统计（含"有标注查询"过滤） |
| `stats_cmirb.py` | CMIRB 7 个子集汇总表 |
| `stats_parquet.py` | C-MTEB qrels + Yale 200 的分级标签统计（需 pyarrow） |
| `stats_extra.py` | PubMedQA gold 摘要数、Yale full/partial 分布（需 pyarrow） |
| `verify_cmirb_gold.py` | 校验 qrels 的 `p_id` 能否 100% 解析回 `corpus.jsonl`，并打印 Q→gold 原文 |
| `samples.py` | 抽取每个数据集的 1 条样例（query + gold 段落），落到 `samples.json` |

---

## 7. 参考链接

- CMIRB / AutoMIR：[arXiv:2410.20050](https://arxiv.org/abs/2410.20050) · [HF 组织](https://huggingface.co/CMIRB) · [C-MTEB/MedicalRetrieval](https://huggingface.co/datasets/C-MTEB/MedicalRetrieval)
- BEIR：[论文 arXiv:2104.08663](https://arxiv.org/abs/2104.08663) · [BeIR/nfcorpus](https://huggingface.co/datasets/BeIR/nfcorpus) · [BeIR/scifact](https://huggingface.co/datasets/BeIR/scifact)
- Yale 200：[HF 数据集](https://huggingface.co/datasets/Yale-BIDS-Chen/medical-rag-expert-evaluation-200) · [arXiv:2511.06738](https://arxiv.org/abs/2511.06738)
- MIRAGE / MedRAG：[GitHub](https://github.com/gzxiong/MIRAGE) · [arXiv:2402.13178](https://arxiv.org/abs/2402.13178) · [MedRAG/textbooks](https://huggingface.co/datasets/MedRAG/textbooks)
- PubMedQA：[HF](https://huggingface.co/datasets/qiaojin/PubMedQA) · [pubmedqa.github.io](https://pubmedqa.github.io/)
- K-QA：[HF](https://huggingface.co/datasets/Itaykhealth/K-QA) · [arXiv:2401.14493](https://arxiv.org/abs/2401.14493)
- BioASQ：[官方数据集门户（需注册）](https://participants-area.bioasq.org/datasets/) · [bigbio 镜像](https://huggingface.co/datasets/bigbio/bioasq_task_b)
- 项目内相关文档：[`diagnosis-retrieval-recall.md`](diagnosis-retrieval-recall.md)（单正例锁死 `Precision@k` 的推导）、
  [`../data/evaluations/medical-health-v2/README.md`](../data/evaluations/medical-health-v2/README.md)（原生题集口径）
