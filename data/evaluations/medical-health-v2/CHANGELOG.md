# 版本记录

## medical-health-v2.0 — 2026-11-14

- 从 v1.0 的 500 题（仅覆盖 7 份语料、89.6% 单证据标注）重构为 **150 题**（`MHE2-0001` ～ `MHE2-0150`），完整覆盖 `data/` 下全部 **35 份** 公开语料。
- 固定配额：120 道 `answerable` + 30 道 `insufficient_evidence`；按 8 大临床/健康领域、6 种题型与 3 档难度均衡分布。
- 为可回答题配备 3–6+ 条去重 `source_refs` 与 `relevance` 分级（`2=core`、`1=supplementary`），并消除跨题共享同一 `anchor_id` 的拆题冗余。
- 为 `insufficient_evidence` 去除提示词模板前缀，引入 `relevance=0` 近邻干扰上下文锚点，支撑 `NDCG@k`、`Core-Recall@k`、`Hit@k` 与 `decoy_abstention_rate` 评估。
