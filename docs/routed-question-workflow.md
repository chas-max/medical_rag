# 路由式问题处理工作流

`medical-answer-expanded` 先用 Qwen 在原问题直通、问题重写、复杂问题分解、假设性回答（HyDE）和抽象问题之间选择一条路径。路由不是节点内部的规则：`question.router` 输出一个 `selector`，四条改写分支各自声明 `activation`（`route` 等于自己的名字才执行），未被选中的分支在运行记录里是 `inactive` 且不调用模型。想只走某一个方法就把 router 和其余分支删掉（见 `medical-answer-decompose`），想完全不提前检索就用 `medical-answer-direct`。原问题通过连线本身参与合流：`merge.collect` 收集所有已连接的问题（含原问题）后交给 `retrieval.multi`，连线数就是检索路数。HyDE 模拟文本只用于向量检索，不作为回答证据，它的输出就是普通 `string`，所以能直接接进 `retrieval.dense`。此流程每次额外调用一次路由模型，非直通时再调用一次转换模型。

调用 `POST /workflows/medical-answer-expanded/runs`，请求体为 `{"inputs":{"question":"高血压患者应如何随访？","provider":"qwen","top_k":5}}`。需配置 `QWEN_API_KEY`，并注意路由与改写会增加延迟及模型调用量。`provider` 现在是可选连线：不接时节点用运行时默认厂商。返回的回答仍须核对原文，不替代专业诊疗。
