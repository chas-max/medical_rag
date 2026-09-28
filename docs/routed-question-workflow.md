# 路由式问题处理工作流

可选的 `medical-answer-expanded` 工作流先用 Qwen 在原问题直通、问题重写、复杂问题分解、假设性回答（HyDE）和抽象问题之间选择一条路径。仅被选中的问题处理节点调用模型；原问题始终参与检索并用于重排与最终回答。HyDE 模拟文本只用于向量检索，不作为回答证据。此流程每次额外调用一次路由模型，非直通时再调用一次转换模型；默认 `/answers` 不变。

调用 `POST /workflows/medical-answer-expanded/runs`，请求体为 `{"inputs":{"question":"高血压患者应如何随访？","provider":"qwen","top_k":5}}`。需配置 `QWEN_API_KEY`，并注意路由与改写会增加延迟及模型调用量。返回的回答仍须核对原文，不替代专业诊疗。
