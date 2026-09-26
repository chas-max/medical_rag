# 医疗 RAG Workflow Engine 与双页面前端实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 FastAPI 医疗 RAG 服务上增加可注册 Node、可验证和可执行的 DAG Workflow Engine，并交付可切换的 ChatGPT 风格问答页与 React Flow 拖拽式工作流编辑器。

**Architecture:** 后端新增 `medical_rag.workflow` 包，使用 Node Registry、Workflow Validator、DAG Executor 和 JSON Repository；现有解析、检索、重排、生成能力包装为内置节点。FastAPI 保留 `/search`、`/answers`、`/documents` 等旧接口，并新增 `/nodes`、`/workflows` 和 `/runs`；前端使用 React、TypeScript、Vite 和 `@xyflow/react`，通过同源 API 调用工作流。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic、pytest、React、TypeScript、Vite、`@xyflow/react`、Vitest、React Testing Library、Playwright smoke test、Milvus、BGE、DeepSeek/Qwen OpenAI-compatible API。

## Global Constraints

- 输入：Markdown、TXT、可提取文字 PDF；不处理扫描版 PDF、URL、图片和患者隐私资料。
- Embedding：`BAAI/bge-small-zh-v1.5`，512 维，向量必须归一化并验证有限数值。
- Dense index：`HNSW_PQ`/`COSINE`，`M=16`、`efConstruction=200`、`m=64`、`nbits=4`；不兼容时明确报错，不静默降级。
- BM25：Milvus 内置 BM25、`chinese` 分析器、`SPARSE_INVERTED_INDEX`/`BM25`；不以其他检索器替代配置要求。
- RRF：固定 `k=60`；重排阈值初始为 `0.5`。
- 生成：DeepSeek 和 Qwen provider、模型 ID、base URL、API Key 独立配置；密钥不进入前端或工作流 JSON。
- 所有 HTTP 响应使用 `{success, data, error}`；未知节点、非法工作流、证据不足和引用校验失败必须安全失败。
- 工作流状态使用 `draft`、`validated`、`published`；只有 `published` 工作流能被问答页选择。
- 现有未跟踪的 `data/` 语料文件不加入任何提交；现有 FastAPI/RAG 测试必须继续通过。
- 新增功能先写失败测试并确认失败，再写最小实现；所有提交前运行完整测试、构建和 `git diff --check`。

## 文件结构

- Create `src/medical_rag/workflow/contracts.py`: Node、ExecutionContext、NodeResult、NodeTrace、PortSchema 和运行状态类型。
- Create `src/medical_rag/workflow/definition.py`: WorkflowDefinition、WorkflowNode、WorkflowEdge、RunRequest 和 Pydantic 校验模型。
- Create `src/medical_rag/workflow/registry.py`: NodeRegistry，负责注册、查找和导出节点元数据。
- Create `src/medical_rag/workflow/validator.py`: 节点、端口、配置、入口、出口、断线和环路校验。
- Create `src/medical_rag/workflow/engine.py`: 拓扑执行、输入汇合、状态追踪、跳过和安全失败。
- Create `src/medical_rag/workflow/repository.py`: `workflows/` JSON 文件仓储和版本保存。
- Create `src/medical_rag/workflow/nodes/`: 输入、文档、检索、融合、证据、生成和输出节点。
- Modify `src/medical_rag/service.py`: 通过默认工作流调用 Engine，保留旧服务方法。
- Modify `src/medical_rag/api.py`: 增加 Workflow 和 Run 路由，统一错误封装，并挂载前端静态资源。
- Create `workflows/medical-answer.json`、`workflows/document-ingestion.json`: 默认发布工作流。
- Create `tests/test_workflow_contracts.py`, `tests/test_workflow_validator.py`, `tests/test_workflow_engine.py`, `tests/test_workflow_api.py`: 后端工作流测试。
- Create `frontend/package.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/index.html`: 前端工程配置。
- Create `frontend/src/api/*`: API 客户端、工作流、运行、文档请求封装。
- Create `frontend/src/workflow/*`: 前端工作流类型、节点类型和连线校验。
- Create `frontend/src/pages/ChatPage.tsx`, `frontend/src/pages/WorkflowEditorPage.tsx`: 两个页面。
- Create `frontend/src/components/chat/*`, `frontend/src/components/workflow/*`: 页面组件。
- Create `frontend/src/test/*`: Vitest 和 React Testing Library 测试。
- Modify `README.md`, `.gitignore`, `pyproject.toml`: 启动、构建、测试和静态资源说明。

---

### Task 1: Define workflow contracts and Node Registry

**Files:**
- Create: `src/medical_rag/workflow/__init__.py`
- Create: `src/medical_rag/workflow/contracts.py`
- Create: `src/medical_rag/workflow/definition.py`
- Create: `src/medical_rag/workflow/registry.py`
- Test: `tests/test_workflow_contracts.py`

**Interfaces:**
- Produces `Node`, `ExecutionContext`, `NodeResult`, `NodeTrace`, `WorkflowDefinition`, `WorkflowNode`, `WorkflowEdge`, `WorkflowRunRequest` and `NodeRegistry` for all later tasks.
- `NodeRegistry.register(node: Node) -> None` rejects duplicate `node.type` with `ValueError`.
- `NodeRegistry.get(node_type: str) -> Node` raises `KeyError` for unknown types.
- `NodeRegistry.metadata() -> list[dict]` returns stable sorted metadata with `type`, `title`, `category`, `inputs`, `outputs` and `config_schema`.

- [ ] **Step 1: Write the failing tests**

```python
def test_registry_exposes_stable_node_metadata():
    registry = NodeRegistry()
    registry.register(StubNode("query.input", "问题输入"))

    assert registry.metadata() == [{
        "type": "query.input",
        "title": "问题输入",
        "category": "input",
        "inputs": {},
        "outputs": {"question": "string"},
        "config_schema": {},
    }]


def test_workflow_rejects_duplicate_node_ids_and_unknown_status():
    with pytest.raises(ValidationError):
        WorkflowDefinition.model_validate({
            "id": "demo", "name": "Demo", "version": 1,
            "status": "unknown", "nodes": [], "edges": []
        })


def test_node_result_records_safe_status_and_outputs():
    result = NodeResult(outputs={"answer": "ok"}, status="success")
    assert result.status == "success"
    assert result.outputs["answer"] == "ok"
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `python -m pytest tests/test_workflow_contracts.py -q`

Expected: FAIL because `medical_rag.workflow` and its contracts do not exist.

- [ ] **Step 3: Implement the minimal contracts**

Use frozen dataclasses for runtime values and Pydantic models for persisted JSON. The core signatures are:

```python
@dataclass(frozen=True)
class NodeTrace:
    node_id: str
    status: Literal["pending", "running", "success", "skipped", "failed"]
    duration_ms: int = 0
    metadata: dict[str, object] = field(default_factory=dict)
    error: dict[str, str] | None = None


@dataclass
class ExecutionContext:
    run_id: str
    workflow_id: str
    variables: dict[str, object]
    trace: list[NodeTrace] = field(default_factory=list)


class Node(Protocol):
    type: str
    title: str
    category: str
    inputs: dict[str, str]
    outputs: dict[str, str]
    config_schema: dict[str, object]

    async def execute(
        self, inputs: dict[str, object], config: dict[str, object],
        context: ExecutionContext,
    ) -> NodeResult: ...
```

Define workflow status as `draft | validated | published`; require unique node IDs and edge IDs; preserve node positions and arbitrary JSON-compatible config.

- [ ] **Step 4: Run the tests to verify green**

Run: `python -m pytest tests/test_workflow_contracts.py -q`

Expected: all contract and registry tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/medical_rag/workflow tests/test_workflow_contracts.py
git commit -m "feat: add workflow contracts and node registry"
```

### Task 2: Implement workflow validation and DAG execution

**Files:**
- Create: `src/medical_rag/workflow/validator.py`
- Create: `src/medical_rag/workflow/engine.py`
- Test: `tests/test_workflow_validator.py`
- Test: `tests/test_workflow_engine.py`

**Interfaces:**
- `validate_workflow(definition: WorkflowDefinition, registry: NodeRegistry) -> list[ValidationIssue]` returns an empty list for valid definitions and stable `code`, `message`, `node_id`, `edge_id` fields for invalid definitions.
- `WorkflowEngine(registry).run(definition, inputs) -> WorkflowRunResult` executes nodes in topological order and returns `run_id`, `status`, `output` and `trace`.
- `WorkflowEngine` never calls a node whose required upstream input is absent; it marks a downstream branch `skipped` when a gate returns `abstained=True`.

- [ ] **Step 1: Write the failing tests**

```python
def test_validator_reports_cycle_and_missing_required_port():
    issues = validate_workflow(cyclic_definition(), registry_with_stubs())
    assert {issue.code for issue in issues} == {"CYCLE", "MISSING_INPUT"}


@pytest.mark.asyncio
async def test_engine_runs_branch_and_join_in_topological_order():
    order = []
    registry = registry_with_recording_nodes(order)
    result = await WorkflowEngine(registry).run(valid_join_definition(), {"question": "高血压"})

    assert result.status == "success"
    assert order == ["query", "dense", "bm25", "fusion", "output"]
    assert result.trace[-1].status == "success"


@pytest.mark.asyncio
async def test_engine_skips_generation_after_evidence_gate_abstains():
    registry = registry_with_gate_and_generation()
    result = await WorkflowEngine(registry).run(gated_definition(), {"question": "未知问题"})

    assert result.output["abstained"] is True
    assert result.trace_by_node("generation").status == "skipped"
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `python -m pytest tests/test_workflow_validator.py tests/test_workflow_engine.py -q`

Expected: FAIL because validator and engine modules do not exist.

- [ ] **Step 3: Implement validator and executor**

Validator must check node types, duplicate IDs, edge endpoints, source/target ports, port type equality, at least one input and output node, required inputs, unreachable nodes and cycles. Engine must use Kahn topological sorting, maintain a map of node outputs, merge fan-in inputs by target port, record monotonic trace order, and convert `ApiError` into a safe failed trace without exposing stack traces.

Use a deterministic run ID from `uuid4().hex`, and use `time.perf_counter()` for each node duration. A failed node returns `status="failed"`; no later node runs. A successful gate with `abstained=True` marks only its downstream generation branch skipped and returns the gate output as the final output.

- [ ] **Step 4: Run the tests to verify green**

Run: `python -m pytest tests/test_workflow_validator.py tests/test_workflow_engine.py -q`

Expected: all validation, ordering, join, failure and skip tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/medical_rag/workflow/validator.py src/medical_rag/workflow/engine.py tests/test_workflow_validator.py tests/test_workflow_engine.py
git commit -m "feat: validate and execute workflow DAGs"
```

### Task 3: Add JSON workflow repository and built-in RAG nodes

**Files:**
- Create: `src/medical_rag/workflow/repository.py`
- Create: `src/medical_rag/workflow/nodes/input.py`
- Create: `src/medical_rag/workflow/nodes/documents.py`
- Create: `src/medical_rag/workflow/nodes/retrieval.py`
- Create: `src/medical_rag/workflow/nodes/evidence.py`
- Create: `src/medical_rag/workflow/nodes/generation.py`
- Create: `src/medical_rag/workflow/nodes/output.py`
- Create: `src/medical_rag/workflow/bootstrap.py`
- Create: `workflows/medical-answer.json`
- Create: `workflows/medical-search.json`
- Create: `workflows/document-ingestion.json`
- Test: `tests/test_workflow_nodes.py`
- Test: `tests/test_workflow_repository.py`

**Interfaces:**
- `WorkflowRepository(root: Path).list() -> list[WorkflowDefinition]`, `.get(workflow_id, version=None) -> WorkflowDefinition`, `.save(definition) -> WorkflowDefinition`.
- `build_registry(store, embedder, reranker, generator) -> NodeRegistry` registers every built-in node.
- Existing `RagService` collaborators are injected; nodes do not instantiate Milvus, embedding or model clients themselves.

- [ ] **Step 1: Write the failing tests**

```python
def test_repository_saves_new_version_without_overwriting_previous(tmp_path):
    repository = WorkflowRepository(tmp_path)
    first = repository.save(valid_published_definition(version=1))
    second = repository.save(valid_published_definition(version=2))

    assert repository.get("medical-answer", version=1).version == 1
    assert repository.get("medical-answer").version == second.version
    assert first.id == second.id == "medical-answer"


@pytest.mark.asyncio
async def test_dense_and_bm25_nodes_delegate_to_injected_store(fake_dependencies):
    registry = build_registry(**fake_dependencies)
    context = ExecutionContext("run", "medical-answer", {})
    result = await registry.get("retrieval.dense").execute(
        {"question": "高血压"}, {"top_k": 5}, context
    )

    assert result.status == "success"
    assert fake_dependencies["store"].dense_calls == 1


@pytest.mark.asyncio
async def test_evidence_gate_abstains_without_calling_generator(fake_dependencies):
    registry = build_registry(**fake_dependencies)
    result = await registry.get("evidence.gate").execute(
        {"evidence": []}, {"threshold": 0.5}, ExecutionContext("run", "w", {})
    )

    assert result.outputs == {"evidence": [], "abstained": True}
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `python -m pytest tests/test_workflow_nodes.py tests/test_workflow_repository.py -q`

Expected: FAIL because the repository, registry bootstrap and built-in nodes do not exist.

- [ ] **Step 3: Implement repository and nodes**

Repository writes `workflows/.index.json` and immutable files named `{workflow_id}.v{version}.json`; `get(id)` returns the highest version. It rejects malformed JSON and duplicate version numbers. The default JSON definitions for `medical-answer`, `medical-search` and `document-ingestion` must connect the ports described in the design and have status `published`.

Node responsibilities are exact: `query.input` returns `question`, `provider`, and `top_k`; dense and BM25 nodes call injected store methods; `retrieval.rrf` reuses the existing RRF behavior; `retrieval.rerank` reuses `Retriever` scoring through injected dependencies; `evidence.gate` emits `abstained`; `generation.llm` calls `GroundedGenerator`; `citation.validate` passes only validated answer fields; document nodes call `parse_document`, `chunk_document`, embedder and store in sequence. The node adapter must convert dataclass values through FastAPI `jsonable_encoder` only at the HTTP boundary.

- [ ] **Step 4: Run the tests to verify green**

Run: `python -m pytest tests/test_workflow_nodes.py tests/test_workflow_repository.py -q`

Expected: all repository versioning, delegation, evidence gate and citation tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/medical_rag/workflow/repository.py src/medical_rag/workflow/nodes src/medical_rag/workflow/bootstrap.py workflows tests/test_workflow_nodes.py tests/test_workflow_repository.py
git commit -m "feat: add built-in medical rag workflow nodes"
```

### Task 4: Expose Workflow API and preserve legacy service routes

**Files:**
- Modify: `src/medical_rag/service.py`
- Modify: `src/medical_rag/api.py`
- Modify: `src/medical_rag/config.py`
- Modify: `tests/test_api.py`
- Create: `tests/test_workflow_api.py`

**Interfaces:**
- `RagService` owns a `WorkflowEngine`, `WorkflowRepository` and default workflow ID while retaining `ingest`, `list_documents`, `search`, `answer` and `health` methods.
- `GET /nodes` returns registered metadata.
- `GET /workflows` returns workflow summaries; `GET /workflows/{id}` returns a full definition.
- `POST /workflows` and `PUT /workflows/{id}` validate and save definitions.
- `POST /workflows/{id}/validate` returns `{valid, issues}`.
- `POST /workflows/{id}/runs` returns a run result; `GET /runs/{run_id}` returns the stored result for the process lifetime.

- [ ] **Step 1: Write the failing API tests**

```python
def test_workflow_api_lists_nodes_and_published_workflows(fake_workflow_service):
    client = TestClient(create_app(service=fake_workflow_service))

    nodes = client.get("/nodes")
    workflows = client.get("/workflows")

    assert nodes.json()["success"] is True
    assert nodes.json()["data"][0]["type"] == "query.input"
    assert workflows.json()["data"][0]["status"] == "published"


def test_workflow_run_rejects_invalid_definition_and_returns_envelope(fake_workflow_service):
    client = TestClient(create_app(service=fake_workflow_service))
    response = client.post("/workflows/medical-answer/runs", json={"inputs": {}})

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_legacy_answer_route_calls_default_workflow(fake_workflow_service):
    client = TestClient(create_app(service=fake_workflow_service))
    response = client.post("/answers", json={"question": "高血压"})

    assert response.status_code == 200
    assert fake_workflow_service.last_workflow_id == "medical-answer"
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `python -m pytest tests/test_workflow_api.py tests/test_api.py -q`

Expected: FAIL because the new routes and workflow-backed service methods do not exist.

- [ ] **Step 3: Implement API and compatibility adapter**

Add Pydantic request models with `inputs: dict[str, object]`, route-level workflow ID checks, and the same `_success`/`_failure` helpers already used by `api.py`. Use an in-process run store for `GET /runs/{run_id}`; return `RUN_NOT_FOUND` with 404 after an unknown ID. Construct real dependencies in lifespan and inject fakes in tests. Legacy `search` calls `medical-search`; legacy `answer` calls `medical-answer`; both retain their existing response data shape.

- [ ] **Step 4: Run the tests to verify green and regression**

Run: `python -m pytest tests/test_workflow_api.py tests/test_api.py tests/test_service.py -q`

Expected: new Workflow API tests and all legacy API/service tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/medical_rag/service.py src/medical_rag/api.py tests/test_api.py tests/test_workflow_api.py
git commit -m "feat: expose workflow runs and preserve legacy rag api"
```

### Task 5: Scaffold React frontend and shared API/types

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/router.tsx`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/workflows.ts`
- Create: `frontend/src/api/runs.ts`
- Create: `frontend/src/api/documents.ts`
- Create: `frontend/src/workflow/types.ts`
- Create: `frontend/src/workflow/nodeTypes.ts`
- Create: `frontend/src/components/AppShell.tsx`
- Create: `frontend/src/components/WorkspaceSwitcher.tsx`
- Create: `frontend/src/styles/global.css`
- Test: `frontend/src/test/api.test.ts`
- Modify: `.gitignore`, `README.md`

**Interfaces:**
- `apiClient<T>(path, init) -> Promise<ApiEnvelope<T>>` rejects non-2xx responses using `{code,message}`.
- `getNodes()`, `listWorkflows()`, `getWorkflow(id)`, `validateWorkflow(id, definition)`, `runWorkflow(id, inputs)`, `listDocuments()` provide typed API calls.
- `WorkflowDefinition`, `WorkflowNode`, `WorkflowEdge`, `NodeMetadata`, `WorkflowRunResult` match backend JSON names exactly.

- [ ] **Step 1: Write the failing frontend API test**

```tsx
it("preserves the success/data/error envelope for workflow requests", async () => {
  server.use(http.get("/nodes", () => HttpResponse.json({
    success: true,
    data: [{ type: "query.input" }],
    error: null,
  })));

  await expect(getNodes()).resolves.toEqual([{ type: "query.input" }]);
});
```

- [ ] **Step 2: Run the test to verify failure**

Run: `npm --prefix frontend test -- --run src/test/api.test.ts`

Expected: FAIL because the frontend package and API client do not exist.

- [ ] **Step 3: Add frontend tooling and shared shell**

Use Vite React TypeScript scripts: `dev`, `build`, `test`, `test:coverage`, `lint`. Add `react`, `react-dom`, `react-router-dom`, `@xyflow/react`, `zod`, `vitest`, `jsdom`, `@testing-library/react`, `@testing-library/user-event`, `msw` and `typescript`. Configure `/api` proxy to `http://127.0.0.1:8000`, but keep API paths without an `/api` prefix so production remains same-origin.

- [ ] **Step 4: Run the test and build**

Run: `npm --prefix frontend test -- --run src/test/api.test.ts`

Expected: PASS.

Run: `npm --prefix frontend run build`

Expected: Vite exits 0 and creates `frontend/dist`.

- [ ] **Step 5: Commit**

```powershell
git add frontend .gitignore README.md
git commit -m "feat: scaffold frontend workflow workspace"
```

### Task 6: Implement ChatGPT-style medical chat page

**Files:**
- Create: `frontend/src/pages/ChatPage.tsx`
- Create: `frontend/src/components/chat/ConversationSidebar.tsx`
- Create: `frontend/src/components/chat/ChatThread.tsx`
- Create: `frontend/src/components/chat/ChatComposer.tsx`
- Create: `frontend/src/components/chat/CitationCard.tsx`
- Create: `frontend/src/components/chat/WorkflowSelector.tsx`
- Modify: `frontend/src/App.tsx`, `frontend/src/styles/global.css`
- Test: `frontend/src/test/chat-page.test.tsx`

**Interfaces:**
- `ChatPage` loads published workflows and submits `{question, provider, top_k}` to `runWorkflow`.
- `ChatThread` renders `answer`, `abstained`, `citations` and selected `trace` without inventing missing values.
- `CitationCard` renders `filename`, `page`, `section`, `excerpt`, `chunk_id` and score when present.

- [ ] **Step 1: Write failing page tests**

```tsx
it("submits a question and renders the verified answer with citations", async () => {
  server.use(
    http.get("/workflows", () => HttpResponse.json(success([publishedWorkflow]))),
    http.post("/workflows/medical-answer/runs", () => HttpResponse.json(success({
      output: {
        answer: "建议按指南进行规律随访。",
        abstained: false,
        citations: [{ chunk_id: "c1", filename: "指南.pdf", page: 4, excerpt: "规律随访" }],
      },
      trace: [],
    })))
  );

  render(<MemoryRouter><ChatPage /></MemoryRouter>);
  await userEvent.type(screen.getByRole("textbox"), "高血压如何随访？");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));

  expect(await screen.findByText("建议按指南进行规律随访。")).toBeInTheDocument();
  expect(screen.getByText("指南.pdf")).toBeInTheDocument();
});


it("renders abstention without an empty or fabricated citation", async () => {
  server.use(
    http.get("/workflows", () => HttpResponse.json(success([publishedWorkflow]))),
    http.post("/workflows/medical-answer/runs", () => HttpResponse.json(success({
      output: {
        answer: "现有资料不足以回答该问题。请查阅可靠医疗资料或咨询专业医务人员。",
        abstained: true,
        citations: [],
      },
      trace: [],
    })))
  );
  render(<MemoryRouter><ChatPage /></MemoryRouter>);
  await userEvent.type(screen.getByRole("textbox"), "没有证据的问题");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(await screen.findByText(/现有资料不足以回答/)).toBeInTheDocument();
  expect(screen.queryByText("指南.pdf")).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `npm --prefix frontend test -- --run src/test/chat-page.test.tsx`

Expected: FAIL because `ChatPage` and chat components do not exist.

- [ ] **Step 3: Implement the page**

Create the ChatGPT-style shell: left conversation list, central message thread, sticky composer, top `WorkspaceSwitcher`, workflow/provider/top_k controls, health badge and medical disclaimer. Keep conversation history in page state only. Disable send while a run is active, show a loading assistant message, and render trace only in an expandable “运行详情” section. Render citation cards from server data and show the fixed abstention message when `abstained` is true.

- [ ] **Step 4: Run tests and production build**

Run: `npm --prefix frontend test -- --run src/test/chat-page.test.tsx`

Expected: all chat tests pass.

Run: `npm --prefix frontend run build`

Expected: Vite exits 0.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/pages/ChatPage.tsx frontend/src/components/chat frontend/src/App.tsx frontend/src/styles/global.css frontend/src/test/chat-page.test.tsx
git commit -m "feat: add medical chat page with citations"
```

### Task 7: Implement drag-and-drop Workflow Editor

**Files:**
- Create: `frontend/src/pages/WorkflowEditorPage.tsx`
- Create: `frontend/src/components/workflow/WorkflowCanvas.tsx`
- Create: `frontend/src/components/workflow/NodePalette.tsx`
- Create: `frontend/src/components/workflow/WorkflowNode.tsx`
- Create: `frontend/src/components/workflow/NodeInspector.tsx`
- Create: `frontend/src/components/workflow/WorkflowToolbar.tsx`
- Create: `frontend/src/components/workflow/RunTracePanel.tsx`
- Create: `frontend/src/components/workflow/WorkflowValidationPanel.tsx`
- Create: `frontend/src/workflow/validation.ts`
- Modify: `frontend/src/router.tsx`, `frontend/src/styles/global.css`
- Test: `frontend/src/test/workflow-editor.test.tsx`

**Interfaces:**
- `WorkflowEditorPage` loads a `WorkflowDefinition`, keeps React Flow nodes/edges as local draft state, and serializes back to the backend schema.
- `isCompatible(sourceType, targetType) -> boolean` rejects incompatible ports.
- `validateClientWorkflow(nodes, edges, metadata) -> ValidationIssue[]` catches missing entry/exit, missing required ports, unknown node types and cycles before the API call.
- `NodePalette` emits a new `WorkflowNode`; `NodeInspector` emits a config patch; `RunTracePanel` renders `WorkflowRunResult.trace`.

- [ ] **Step 1: Write failing editor tests**

```tsx
it("adds a node from the palette and serializes its position and config", async () => {
  render(<MemoryRouter initialEntries={["/workflows/medical-answer"]}><WorkflowEditorPage /></MemoryRouter>);

  await userEvent.click(screen.getByRole("button", { name: "添加向量检索" }));
  expect(screen.getByText("向量检索")).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "保存" })).toBeEnabled();
});


it("blocks a connection when output and input port types differ", async () => {
  expect(isCompatible("string", "evidence")).toBe(false);
  expect(isCompatible("evidence", "evidence")).toBe(true);
});


it("shows node failure details in the run trace", async () => {
  server.use(http.post("/workflows/medical-answer/runs", () => HttpResponse.json(success({
    output: { answer: "", abstained: true, citations: [] },
    trace: [{
      node_id: "rerank",
      status: "failed",
      error: { code: "MODEL_ERROR", message: "重排不可用" },
    }],
  }))));
  render(<MemoryRouter><WorkflowEditorPage /></MemoryRouter>);
  await userEvent.click(screen.getByRole("button", { name: "运行" }));
  expect(await screen.findByText("重排不可用")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `npm --prefix frontend test -- --run src/test/workflow-editor.test.tsx`

Expected: FAIL because the editor page, canvas and node components do not exist.

- [ ] **Step 3: Implement the editor**

Use React Flow custom nodes with handles keyed by backend port names. Dragging a palette item creates a node with a generated ID, default config from `config_schema`, and the pointer location. Convert React Flow edges to `{source, source_port, target, target_port}` before save. Toolbar actions are `验证`, `保存`, `运行`, `自动布局`; save is disabled when client validation has issues. The inspector renders string, number, boolean and enum controls from the node schema. Use status colors pending gray, running blue, success green, skipped yellow and failed red. Keep workflow secrets out of inspector fields.

- [ ] **Step 4: Run tests and build**

Run: `npm --prefix frontend test -- --run src/test/workflow-editor.test.tsx`

Expected: all editor tests pass.

Run: `npm --prefix frontend run build`

Expected: Vite exits 0.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/pages/WorkflowEditorPage.tsx frontend/src/components/workflow frontend/src/workflow/validation.ts frontend/src/router.tsx frontend/src/styles/global.css frontend/src/test/workflow-editor.test.tsx
git commit -m "feat: add drag and drop workflow editor"
```

### Task 8: Static hosting, end-to-end verification and documentation

**Files:**
- Modify: `src/medical_rag/api.py`
- Modify: `README.md`
- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Create: `tests/test_frontend_hosting.py`
- Create: `frontend/src/test/smoke.spec.ts`

**Interfaces:**
- FastAPI serves `frontend/dist` at `/ui` and returns the SPA entry for `/ui/chat` and `/ui/workflows/{id}` without intercepting API routes.
- `npm run build` produces the directory expected by the FastAPI static mount.
- README documents backend, frontend, Workflow JSON, dev proxy, production static hosting and verification commands.

- [ ] **Step 1: Write failing hosting and smoke tests**

```python
def test_spa_routes_serve_html_without_replacing_api_errors(tmp_path):
    client = TestClient(create_app(service=FakeService(), frontend_dist=tmp_path))
    (tmp_path / "index.html").write_text("<div id='root'></div>", encoding="utf-8")

    assert client.get("/ui/chat").status_code == 200
    assert "root" in client.get("/ui/chat").text
    assert client.get("/health").json()["success"] is True
```

Add a Playwright smoke test that opens `/ui/chat`, switches to `/ui/workflows/medical-answer`, sees the canvas, returns to `/ui/chat`, submits a mocked question, and sees a citation card. Run it only when a local frontend/backend server is available.

- [ ] **Step 2: Run the tests to verify failure**

Run: `python -m pytest tests/test_frontend_hosting.py -q`

Expected: FAIL because FastAPI does not mount the built frontend.

- [ ] **Step 3: Implement hosting and documentation**

Mount `StaticFiles(directory=settings.frontend_dist, html=True)` only when the directory exists. Add a `/ui/{path:path}` fallback that serves `index.html` for known SPA routes and leaves `/health`, `/documents`, `/search`, `/answers`, `/nodes`, `/workflows` and `/runs` to their API handlers. Document PowerShell commands:

```powershell
python -m pip install -e ".[dev]"
npm --prefix frontend install
npm --prefix frontend run build
uvicorn medical_rag.api:app --app-dir src --reload
```

Document `npm --prefix frontend run dev` for development, `npm --prefix frontend test -- --run`, `npm --prefix frontend run build`, backend coverage, and the fact that Milvus and paid model calls remain environment-dependent.

- [ ] **Step 4: Run the complete verification suite**

Run: `python -m pytest -q --cov=medical_rag --cov-report=term-missing`

Expected: all backend tests pass and coverage is above 80% for covered project modules; the opt-in Milvus test may be skipped when Docker is unavailable.

Run: `npm --prefix frontend test -- --run --coverage`

Expected: all frontend unit tests pass with coverage output.

Run: `npm --prefix frontend run build`

Expected: exit code 0 and `frontend/dist/index.html` exists.

Run: `python -m compileall -q src; git diff --check`

Expected: both commands exit 0 with no whitespace errors.

- [ ] **Step 5: Commit**

```powershell
git add src/medical_rag/api.py tests/test_frontend_hosting.py frontend README.md .gitignore pyproject.toml
git commit -m "feat: host workflow frontend and document local usage"
```

## Plan Self-Review

- Spec coverage: Node protocol and Registry are Task 1; validation and execution are Task 2; JSON versions and built-in RAG nodes are Task 3; new APIs and legacy compatibility are Task 4; shared frontend and routing are Task 5; Chat page is Task 6; drag-and-drop editor is Task 7; static hosting, tests and documentation are Task 8.
- Placeholder scan: no `TODO`, `TBD`, unassigned implementation step or unspecified error-handling step remains.
- Type consistency: backend uses `WorkflowDefinition`, `WorkflowNode`, `WorkflowEdge`, `WorkflowRunResult`, `NodeMetadata`; frontend uses the same serialized field names. `runWorkflow` consumes `{inputs}` and returns `output` plus `trace`, matching the API task.
- Scope check: the plan covers one connected product with two UI surfaces over one Engine; authentication, multi-tenant and OCR remain explicitly out of scope.
