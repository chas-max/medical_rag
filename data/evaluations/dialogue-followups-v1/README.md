# 结构化追问合成评测集 v1

固定合成数据，不使用真实患者内容，也不在运行时随机生成病例。

## 结构

- `version`: 1
- `cases`: 恰好 40 组，每组 3–6 轮。
- 每组 `id` 唯一，覆盖 `topic-*`、`intent-*`、`continuity-*`、`safety-*`、
  `isolation-*`、`ambiguity-*` 六类。
- 恰好 10 组含 `must_clarify: true` 的轮次（全部在 `ambiguity-*`）。

每轮字段：

| 字段 | 含义 |
|---|---|
| `question` | 用户本轮原话（合成） |
| `topic` | 期望讨论对象；`null` 表示完整问题不压缩成单一主题 |
| `intent` | 期望具体意图（枚举见设计文档 5.1） |
| `must_clarify` | 该轮是否必须澄清而不是作答 |
| `required_terms` | 完整问题必须包含的词 |
| `intent_terms` | 完整问题至少包含一个的词 |
| `forbidden_terms` | 完整问题不得出现的词（亚型、剂量、诊断断言等） |
| `replay` | 仅离线契约测试使用的解析器输出夹具；live 模式不读取 |

## 判据

`score_turn` 只做确定性筛查：`required_terms` 全部出现、`intent_terms` 至少一个、
`forbidden_terms` 均不出现，再比较主题、意图与是否澄清。

这不是医学事实校验，也不把逐字相等当成语义正确。live 模式的部分必须由人工复核
原意与约束是否保留。

## 运行

```powershell
.\.venv\Scripts\python.exe -B -m medical_rag.dialogue_evaluation --cases data/evaluations/dialogue-followups-v1/cases.json --mode replay
```

live 模式需要显式加 `--allow-paid-model`，否则在构造任何模型客户端之前拒绝执行。

## 覆盖

| IDs | 组数 | 覆盖 |
|---|---:|---|
| topic-01..08 | 8 | 示例三轮、疾病切换、生活方式意图继承、陌生对象 |
| intent-01..06 | 6 | 病因/症状/预防/诊断/治疗切换与显式更正 |
| continuity-01..06 | 6 | 超窗口主题、无摘要恢复、长对话、多子问题 |
| safety-01..06 | 6 | 长问题、历史注入、亚型/剂量/诊断不得新增 |
| isolation-01..04 | 4 | 会话内独立性、对象隔离 |
| ambiguity-01..10 | 10 | 双病、双药、无历史代词、对象/意图不明及简短澄清回复 |
