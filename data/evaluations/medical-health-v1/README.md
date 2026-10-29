# 公开医疗健康知识评测集 v1.0

本目录固定 500 道中文问题，面向本项目公开语料的检索、回答忠实度、证据不足拒答与医疗安全评测。它不是临床决策或诊疗能力认证。题库源为 `questions.jsonl`；`drafts/` 保留逐条摘录和人工编排依据，跨主题题由两份以上来源的已核验摘录合成。`manifest.json` 固定配额和 SHA-256；`rubric.md` 是给 ChatGPT、Claude Code、Qoder 的统一 0–4 分裁判协议。

## 来源与版本

七份原始 PDF/Markdown 随仓库置于 `data/`，文件名见 `source-registry.json`。它们不是本题库的生成物；迁移后必须保持原始字节与注册哈希一致。2017 年基本公卫规范与 2025 年基层高血压指南的年龄范围、目标值并不相同，涉及它们的题目明确标出版本；不能当作今天所有患者通用的个体化指令。每题 `source_refs` 记录源 ID、PDF 的一基页码或 Markdown 标题、原文摘录及分块无关的稳定锚点。PDF 页码是 PDF 文件页序，不一定等于印刷页码。

## 离线校验与修订

在仓库根目录运行：

```powershell
python data/evaluations/medical-health-v1/tools/validate_dataset.py --dataset data/evaluations/medical-health-v1/questions.jsonl --manifest data/evaluations/medical-health-v1/manifest.json --registry data/evaluations/medical-health-v1/source-registry.json
```

校验检查 500 题、四类配额、重复题、来源 ID 与原文件哈希、题库/裁判协议/注册表哈希。草稿可用 `medical_rag.evaluation_release_build.compile_release` 重新编译；编译器核对原文摘录位置并拒绝覆盖已有发布文件。修订时另存新版本，更新 `CHANGELOG.md`、哈希和清单；不要原地覆盖旧版以保持历史分数可复现。新增问题/参考答案须由具备相应知识的人员逐条复核，自动校验原文包含关系不能证明医学语义正确。

## 入库与分块映射

把七份来源按注册表 `source_id` 上传入库（API 的 `source_id` 表单字段）；不要只依赖上传文件名或随机生成的文档 ID。取本次实际入库的块清单为 UTF-8 JSON 数组，每项至少有 `id`、`source_id`、`sha256`（实际索引的源文件哈希）、`text`、`page`（PDF 一基页码，Markdown 为 `null` 或 Milvus 中的 `0`）、`section`（Markdown 标题路径，PDF 可为空）和 `parent_text`（父子分块下的父块全文；没有父块时可省略）。例如：

```json
[{"id":"当前索引真实块ID","source_id":"primary-hypertension-2025","sha256":"与注册表相同的64位SHA-256","text":"本次入库子块全文","parent_text":"包含该子块的父块全文","page":4,"section":""}]
```

以实际索引内容为准，不要把样例块 ID 当成金标准。映射要求同来源、同定位、同源文件哈希，且去除排版空白后原文摘录须按原字序完整出现：先匹配子块 `text`，找不到时再匹配 `parent_text`，并把同属该父块的全部子块列为正相关块（方法记为 `parent_containment` / `parent_containment_multi`）。这是父子分块的有意行为——生成时拿到的是父块，命中任一子块都会把该段上下文带给模型；同一摘录被多个重叠块完整包含时，也全部列为正相关块。若摘录跨父块、无法完整定位或源文件哈希变化，映射命令会失败，需人工检查分块策略及锚点，不应强行猜配：

```powershell
python data/evaluations/medical-health-v1/tools/remap_chunk_ids.py --dataset data/evaluations/medical-health-v1/questions.jsonl --chunks .\chunks.json --registry data/evaluations/medical-health-v1/source-registry.json --output .\medical-health-runtime.jsonl
```

成功时另有 `.mapping.json` 旁路报告，保留匹配方法与置信度。`questions.jsonl` 中的 `relevant_chunk_ids` 故意为空；评估只能使用已经完成当前索引映射的 runtime 副本。20 道 `insufficient_evidence` 题不设正相关块，不参与普通 Recall/MAP 分母。

评估命令在启动模型调用前会重新核对发布题库、裁判协议、注册表及全部七份源文件的 SHA-256，并要求运行时题目除相关块 ID 外与发布题库逐条相同。它不会自动查询当前 Milvus 索引；换库或重分块后必须重新导出真实 chunk 清单并重新映射。

## 运行与三平台评分

以下模型/API 调用可能计费，只有显式运行才发生：

```powershell
python -m medical_rag.evaluate --dataset .\medical-health-runtime.jsonl --manifest data/evaluations/medical-health-v1/manifest.json --output .\rag-report.json --top-k 5 --answer-provider qwen --judge-provider qwen
python data/evaluations/medical-health-v1/tools/export_for_judge.py --report .\rag-report.json --dataset data/evaluations/medical-health-v1/questions.jsonl --output .\judge-input.jsonl
```

现有本地评估的检索 Precision/Recall/F1/MAP/MRR 和三项 0–1 语义评分单独报告；后者不是跨平台正式答案分。把同一份 `judge-input.jsonl` 与 `rubric.md` 分别交给 ChatGPT、Claude Code、Qoder 独立判分。先用 `Get-FileHash .\judge-input.jsonl -Algorithm SHA256` 计算完整输入文件哈希；三家逐题输出均须填写相同的 `judge_input_sha256`。各平台输出一份逐题 JSONL，严格按 rubric 的 JSON 字段填写，平台标识分别为 `chatgpt`、`claude`、`qoder`，`rubric_version` 为 `medical-health-v1.0`，模型标识写全。不得让裁判借外部医学常识补齐本题库固定证据，也不得输入患者资料或密钥。

```powershell
python data/evaluations/medical-health-v1/tools/aggregate_judges.py --dataset data/evaluations/medical-health-v1/questions.jsonl --judge-input .\judge-input.jsonl --chatgpt .\chatgpt-judged.jsonl --claude .\claude-judged.jsonl --qoder .\qoder-judged.jsonl --output .\judge-summary.json
```

汇总器逐维取三家中位数，权重为关键要点 35%、忠实度 30%、相关性 20%、医疗安全 15%，给 480 道可回答题宏平均的 0–100 答案分。某维最大与最小相差至少 2 分即列入人工复核；裁判不全的题不进入答案均分。证据不足题单列正确拒答率、无依据回答与安全事件，不折入答案分。正式报告应同时展示题库及语料哈希、检索指标、答案分、有效题数、拒答、安全和分歧数量；不同版本不直接比较。

原始评估报告含问题、证据全文与回答；只保存在受控位置，避免提交敏感资料。当前仓库没有自动调用三家外部裁判，也没有真实索引的预置 chunk 清单，不能仅凭这套静态题库宣称已有模型最终得分。
