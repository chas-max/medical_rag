# 医疗健康 RAG 评测集 v2（150 题分级多证据与安全拒答增强）设计

日期：2026-11-14（Asia/Shanghai）

状态：待用户审阅确认（已完成需求澄清与规模调整：由 100 题升级为 150 题，强化医疗安全与拒答测试并保持高密度检索评估）。

范围：设计文档，尚未开始业务代码修改或题库生成实现。

---

## 1. 目标与已确认决策

本设计旨在修复 `data/evaluations/medical-health-v1/`（500 题）存在的评测失真与语料覆盖缺口，构建独立版本化评测集 **`data/evaluations/medical-health-v2/`（150 题）**，并向后兼容升级 `src/medical_rag/` 评测模块：

1. **全量语料覆盖**：覆盖 [data/](file:///d:/medical-rag-resume/data/) 目录下实测存在的全部 **35 份** 公开医疗健康文档（1 份 `.md` + 34 份 `.pdf`），解决 v1 仅覆盖 7 份文档（20%）的问题。
2. **高密度分级检索标注（120 道可回答题）**：每道可回答题标注 **3–6+ 条去重 `source_refs`**（含必须命中的核心证据与等价/补充出处），并引入分级相关性 `relevance`（`2=core`、`1=supplementary`），目标重映射后每题关联 **4–10 个 `relevant_chunk_ids`**，消除 v1 单正例导致的 `Precision@5` 上限锁死（0.20）、`Recall@5` 二值跳变和等价块假阴性惩罚。
3. **医疗安全与边界拒答强化（30 道拒答题）**：将 `insufficient_evidence` 占比从 v1 的 4%（20/500）提升至 **20%（30/150）**，去除明显模板前缀，引入近邻相关干扰锚点（`relevance: 0`），检验 RAG 系统在召回相近医学片段后能否守住处方、剂量、缺失前提与未收录边界。
4. **向后兼容现有评测工具链**：保持 `medical-health-v1/` 目录及全部现有 v1 测试、SHA-256 锁定不受破坏；`python -m medical_rag.evaluate`、`remap_chunk_ids.py`、`export_for_judge.py`、`aggregate_judges.py` 均同时支持 v1 与 v2。

非目标：不替换现有 Milvus 向量库或在线检索工作流实现；不修改 `medical-health-v1/` 现有题库与校验哈希；不将评测分等价为临床执业能力认证。

---

## 2. 现有 500 题测试集核心问题依据

来自当前工作区实测统计（`questions.jsonl`、`evaluation_metrics.py`、`evaluation_tools.py`、`evaluate.py`）：

1. **相关证据极度稀疏**：500 题中 `448` 题（89.6%）仅 1 条 `source_refs`；经默认分块重映射后，480 道可回答题中有 `431` 题（89.8%）仅映射出 `1` 个 `relevant_chunk_ids`、`47` 题有 `2` 个、`2` 题有 `3` 个。
2. **语料覆盖严重不足**：`data/` 下有 35 份语料，v1 `source-registry.json` 仅使用 7 份，缺失慢阻肺、流感、支原体肺炎、四大肿瘤、精神障碍、2024 版高血压/糖尿病临床指南及 7 种其他食养指南。
3. **冗余拆题严重**：共有 `73` 个完全相同的 `anchor_id` 被重复引用 `170` 次（例如同一句膳食推荐被拆成 `MHE-0005/0006/0007` 三道细碎题）。
4. **对比题与多证据题名不副实**：40 道 `comparison` 题中有 `31` 题仅引用 1 条 `source_refs`；500 题中仅 `20` 题（4%）跨 2 个来源。
5. **拒答题特征明显且契约断裂**：20 道 `insufficient_evidence` 多以“仅凭……能否……”开头，且为满足 `evaluation_dataset.py` 非空 `source_refs` 校验强行挂载一条引用并在 `remap_release()` 中直接跳过。
6. **指标无相关性分级**：`evaluation_metrics.py` 仅支持无权重二元集合，无法区分核心必中证据与补充佐证，也未计算 `NDCG@k` 与核心证据召回率。

---

## 3. 备选方案与取舍

| 方案 | 优点 | 局限 | 结论 |
| --- | --- | --- | --- |
| **方案 A（采用）**：新建 `medical-health-v2/` + 向后兼容升级评测内核（支持 `relevance` 分级、`NDCG@k`、`Core-Recall@k` 与近邻干扰校验） | 彻底解决 Precision/Recall 精度失真；区分核心/补充证据；完整兼容 v1 与现有 CLI/裁判管线 | 需小幅修改 `evaluation_*.py` 5 个模块并补充单测 | **采用** |
| **方案 B**：仅扩充 v2 数据标注，不改指标与分级字段 | 零代码改动 | 无法区分关键阈值块与辅助背景块；无法对拒答题做近邻干扰命中抵抗率统计 | 不采用 |
| **方案 C**：设计脱离 `evaluate.py` 的全新 JSON 评测格式 | Schema 完全自由 | 无法复用现有检索/工作流离线评估入口及三平台裁判导出汇总脚本 | 不采用 |

---

## 4. 目录结构与版本规范

新建 `data/evaluations/medical-health-v2/`，与 `medical-health-v1/` 完全隔离：

```text
data/evaluations/medical-health-v2/
├── CHANGELOG.md
├── README.md
├── manifest.json          # dataset_version="medical-health-v2.0", question_count=150 及配额/SHA-256
├── rubric.md              # v2.0 跨平台裁判协议（补充分级证据与安全拒答判例）
├── source-registry.json   # 登记 data/ 下全部 35 份语料的 source_id、相对路径与 SHA-256
├── questions.jsonl        # 150 道主评测题（MHE2-0001 ~ MHE2-0150，relevant_chunk_ids=[]）
├── drafts/                # 原始摘录与编制映射记录
└── tools/                 # 复用 medical_rag.evaluation_tools 的命令行入口
```

---

## 5. 数据 Schema 与分级多证据模型

### 5.1 题库单行结构（兼容 `evaluation_dataset.py`）

保留现有全部顶层字段，新增**可选**派生字段与引用分级字段：

```json
{
  "id": "MHE2-0001",
  "question": "...",
  "reference_answer": "...",
  "key_points": ["要点1", "要点2", "要点3"],
  "answerability": "answerable",
  "category": "心血管诊疗与食养",
  "question_type": "comparison",
  "difficulty": "hard",
  "source_refs": [
    {
      "source_id": "hypertension-guideline-2024",
      "locator": {"page": 12},
      "quote": "...",
      "anchor_id": "hypertension-guideline-2024#page-12#...",
      "relevance": 2
    },
    {
      "source_id": "primary-hypertension-2025",
      "locator": {"page": 5},
      "quote": "...",
      "anchor_id": "primary-hypertension-2025#page-5#...",
      "relevance": 1
    }
  ],
  "relevant_chunk_ids": [],
  "chunk_grades": {},
  "context_chunk_ids": []
}
```

### 5.2 `relevance` 分级定义

| 值 | 语义标签 | 归属行为（`remap_release`） | 适用场景 |
| ---: | --- | --- | --- |
| `2` | `core`（核心必中证据） | 写入 `relevant_chunk_ids`，在 `chunk_grades` 中记为 `2`（默认缺省值，保证 v1 兼容） | 回答关键要点、关键数值、对比主体或安全警示所**必须依据**的原文段落 |
| `1` | `supplementary`（等价/补充证据） | 写入 `relevant_chunk_ids`，在 `chunk_grades` 中记为 `1`（若同块已被 `2` 命中则取最高分 `2`） | 同文档其他章节复述、配套《问答》手册、跨指南等价表达或背景支撑段落 |
| `0` | `context_only`（近邻干扰上下文） | **不写入** `relevant_chunk_ids`，写入可选 `context_chunk_ids` 并在 `.mapping.json` 记为 `context_only` | 专用于 `insufficient_evidence` 题目，标注与问题字面高度相关但不足以支撑个体化或超纲结论的陷阱段落 |

### 5.3 校验规则约束

- **可回答题（`answerable`）**：
  - 至少包含 `1` 条 `relevance == 2` 的核心引用；
  - 在 v2 题库建设规范中，每道 `answerable` 题标注 **3–6+ 条** 去重 `source_refs`（不同 `anchor_id`），且不得在多题间大量复用完全相同的 `anchor_id`。
- **拒答题（`insufficient_evidence`）**：
  - 允许 `source_refs` 为 `[]`，或仅包含 `relevance == 0` 的近邻干扰锚点（校验器仍验证 `relevance == 0` 锚点的原文摘录与 `anchor_id` 真实存在于对应源文件页码/标题中，杜绝编造）；
  - 严禁 `insufficient_evidence` 出现 `relevance >= 1`（仅限 v2 或显式声明 `relevance` 时生效，不破坏 v1 历史文件兼容）。

---

## 6. 150 题配额与全 35 份语料覆盖设计

### 6.1 可回答性与医疗安全结构（120 + 30）

- **`answerable`：120 题（80%）**
  - 其中包含约 **15–20 题** 含明确**医疗红线/转诊与紧急就医指征**的高安全权重检索题（如高血压急症、低血糖急救、重症支原体肺炎指征、精神障碍自伤/暴力风险评估、肿瘤危急征象等），既测检索精度也测 `medical_safety` 要点覆盖。
- **`insufficient_evidence`：30 题（20%）**
  - 统一使用自然患者/基层医生口吻提问，**禁用**“仅凭这份指南能否……”等提示词模板，均匀覆盖五类真实安全边界：
    1. **擅自停药/替换处方治疗**（6 题）：诱导停用降压药、降糖药或抗精神病药改纯饮食/食药物质控制。
    2. **个体化处方药剂量或有创操作指令**（6 题）：要求直接给出具体患者用药剂量、调药方案或手术/放化疗个体决策。
    3. **关键临床前提缺失/多病禁忌冲突**（6 题）：合并肾衰、妊娠、儿童或未提供分期/化验单下索要定量补钾、限蛋白或具体食谱。
    4. **近邻相关片段诱导外推**（6 题，均配 `relevance: 0` 上下文锚点）：检索器会高概率召回一般成人或正常人群建议，测试系统能否识别适用人群与限制条件并拒答。
    5. **语料未收录领域或跨版本混用前提**（6 题）：涉及 35 份语料明确未覆盖的指南（如急性缺血性卒中溶栓、支气管哮喘急性发作等）或用旧版公卫边界替代新版临床标准。

### 6.2 八大领域与 35 份语料全覆盖配额（合计 150 题）

覆盖 `data/` 下全部 35 份文件，且**每份文件至少被 ≥3 条 `source_refs` 命中**：

| 领域 (`category`) | 涵盖文件（共 35 份） | 总题数 | 可回答 | 拒答 |
| --- | --- | ---: | ---: | ---: |
| 公共卫生与健康素养 | 《国家基本公共卫生服务规范（第三版）》《中国公民健康素养（2024年版）》（2 份） | 16 | 13 | 3 |
| 心血管诊疗与食养 | 《中国高血压防治指南（2024年修订版）》《国家基层高血压防治管理指南（2025版）》《成人高血压食养指南（2023）》正文+问答（4 份） | 20 | 16 | 4 |
| 内分泌代谢与体重管理 | 《中国糖尿病防治指南（2024版）》《肥胖症诊疗指南（2024）》《体重管理指导原则（2024）》《成人糖尿病/肥胖/高脂血症/高尿酸血症与痛风食养指南》正文+问答（11 份） | 26 | 21 | 5 |
| 呼吸、传染病与儿科 | 《中国慢阻肺基层诊疗与管理指南（2024）》《流行性感冒诊疗方案（2025）》《儿童肺炎支原体肺炎诊疗指南（2025）》《儿童青少年生长迟缓食养指南（2023）》（4 份） | 18 | 14 | 4 |
| 恶性肿瘤规范诊疗 | 《乳腺癌（2022）》《宫颈癌（2022）》《原发性肺癌（2022）》《原发性肝癌（2024）》诊疗指南（4 份） | 16 | 12 | 4 |
| 精神神经与老年/肾脏健康 | 《精神障碍诊疗规范（2020）》《成人慢性肾脏病（2024）》《成人脑卒中/骨质疏松症/肌少症（2026）》食养指南正文+问答（9 份） | 20 | 15 | 5 |
| 膳食营养基础准则 | 《中国居民膳食指南2022_平衡膳食八准则.md》及其与其他食养指南衔接（1 份） | 10 | 9 | 1 |
| 跨主题综合与多版本对比 | 跨疾病共病管理、“临床指南 vs 食养指南”协同、“2017 公卫 vs 2025 基层”差异对照（跨多份文档） | 24 | 20 | 4 |
| **合计** | **35 份语料 100% 覆盖** | **150** | **120** | **30** |

### 6.3 题型与难度分布（合计 150 题）

- **`question_type`**：
  - `multi_evidence`：**40**（全部 `answerable`，跨章节或跨 ≥2 份文档）
  - `comparison`：**24**（22 `answerable` + 2 `insufficient_evidence`；可回答对比题必须含 ≥2 个独立对比来源锚点）
  - `scenario`：**42**（20 `answerable` + 22 `insufficient_evidence`）
  - `procedure`：**18**（15 `answerable` + 3 `insufficient_evidence`，防止模型按题型猜拒答）
  - `numeric`：**16**（13 `answerable` + 3 `insufficient_evidence`，测数值检索与条件缺失下拒绝编造数值）
  - `fact`：**10**（10 `answerable`）
- **`difficulty`**：`easy: 25`、`medium: 70`、`hard: 55`

---

## 7. `src/medical_rag/` 评测模块向后兼容升级

1. **[evaluation_dataset.py](file:///d:/medical-rag-resume/src/medical_rag/evaluation_dataset.py)**
   - 校验 `source_refs[].relevance`（可选，允许 `0, 1, 2`；未传时不报错，下游按 `2` 处理）。
   - 允许 `insufficient_evidence` 行的 `source_refs` 为 `[]` 或全部为 `relevance == 0`；要求 `answerable` 行至少存在 1 条 `relevance != 0` 的引用。
   - 支持可选 `chunk_grades: dict[str, int]`（键非空、值为 `1` 或 `2`）和 `context_chunk_ids: list[str]`（去重非空字符串）。
2. **[evaluation_tools.py](file:///d:/medical-rag-resume/src/medical_rag/evaluation_tools.py)**
   - `validate_release()`：校验 `manifest.json`、35 份语料 SHA-256、150 题配额、以及每条 `source_ref`（含 `relevance == 0` 上下文锚点）的原文包含关系与 `anchor_id`。
   - `remap_release()`：
     - `answerable` 题中所有 `relevance >= 1` 的引用解析为 `relevant_chunk_ids` 并生成 `chunk_grades`（取最高级别）；
     - `insufficient_evidence` 题保留 `relevant_chunk_ids = []`，若有 `relevance == 0` 的锚点则解析并写入 `context_chunk_ids`，旁路 `.mapping.json` 记录为 `context_only`（无锚点则记为 `skipped`）。
3. **[evaluation_metrics.py](file:///d:/medical-rag-resume/src/medical_rag/evaluation_metrics.py)**
   - 扩展 `retrieval_scores(ranked_ids, relevant_ids, k, *, chunk_grades=None)`：
     - 保留原有 `precision`、`recall`、`f1`、`ap`、`rr`、`retrieved_count`、`hit_ids` 计算与返回值结构完全不变；
     - 新增 `ndcg`（使用增益 $\text{gain} = 2^{\text{grade}} - 1$，即 `core=3`、`supplementary=1`、`non-relevant=0` 计算 $\text{DCG@k} / \text{IDCG@k}$）、`core_recall`（`grade == 2` 集合在 Top-k 中的召回率；若未提供 `chunk_grades` 或无 `grade == 2` 块则回退等同 `recall`，避免除零）与 `hit_rate`（Top-k 是否至少命中 1 个相关块，`1.0` 或 `0.0`）。
4. **[evaluation_report.py](file:///d:/medical-rag-resume/src/medical_rag/evaluation_report.py)**
   - `new_case()` 与 `score_evidence()` 透传 `chunk_grades` 与 `context_chunk_ids`，记录拒答题当次检索命中的近邻干扰块数（`context_hit_count`）。
   - `build_report()` 保留原有全部 `summary` 字段，新增汇总 `ndcg`、`core_recall`、`hit_rate`，以及 `decoy_abstention_count` / `decoy_abstention_rate`（仅在 `context_hit_count > 0` 的拒答题上统计正确拒答率，无样本时为 `None`）。
5. **[evaluate.py](file:///d:/medical-rag-resume/src/medical_rag/evaluate.py)**
   - 在运行时与发布版一致性比对（第 88–92 行）时，忽略派生映射字段 `{"relevant_chunk_ids", "chunk_grades", "context_chunk_ids"}`，确保经过 `remap_chunk_ids.py` 生成的 v2 运行时文件可直接通过 `evaluate.py` 校验。

---

## 8. 质量门禁、反幻觉与测试验证

1. **反幻觉与零漂移红线**：编制 150 题时，使用本地脚本直接读取 `data/` 下 35 份原始文件提取对应页码/标题原文，全部 `quote`、`locator`、`anchor_id` 必须通过 `validate_release()` 100% 精确验证；禁止手写凭记忆估算的页码或摘录。
2. **默认分块全量重映射验证**：新增针对 `medical-health-v2` 的集成测试，确认全部 **150 题** 在项目当前默认 `ingest.py`（`parse_document → chunk_document`）分块下 **100% 重映射无报错**，且可回答题平均 `relevant_chunk_ids` 达到预期的多块覆盖密度。
3. **回归门禁**：所有新建与修改的评测模块测试（`test_evaluation_*.py`）与既有 `v1` 非集成测试均须全部通过；所有新生成报告指标与题库统计均以实跑命令输出为准。
