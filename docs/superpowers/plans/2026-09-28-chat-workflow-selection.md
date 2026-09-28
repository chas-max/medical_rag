# 对话界面工作流选择 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让问答页直接选择可完成“检索→证据→生成→答案输出”的已发布工作流，并移除工作流编辑器中的对话测试入口，同时保留检索和文档入库后台功能。

**Architecture:** 在前端新增纯函数 `isAnswerWorkflow`，依据节点类型和有向边判断回答工作流，问答页加载全部已发布工作流后只展示符合条件的项。编辑器继续加载和编辑全部工作流，但仅保留校验记录，不再执行测试问题；后端接口和工作流 JSON 不删除。

**Tech Stack:** React 19, TypeScript, React Router, Vitest, Testing Library, Vite。

## Global Constraints

- `GET /workflows` 继续返回全部已发布流程，后台 `/search`、`/documents` 功能不变。
- 可选回答流程必须存在从 `query.input` 到 `answer.output` 的有向路径，并经过检索、`evidence.gate`、`generation.llm`、`citation.validate`。
- 工作流切换只影响后续发送的问题，不修改已有本地对话消息。
- 新增行为遵循 TDD：先写失败测试，再写最小实现并运行测试。

---

### Task 1: 添加回答工作流筛选函数

**Files:**
- Create: `frontend/src/workflow/selection.ts`
- Test: `frontend/src/test/selection.test.ts`

**Interfaces:**
- Produces `isAnswerWorkflow(workflow: Workflow): boolean` and `selectAnswerWorkflows(workflows: Workflow[]): Workflow[]` for `ChatPage`.

- [ ] **Step 1: Write the failing test**

```ts
import { expect, it } from "vitest";
import { isAnswerWorkflow, selectAnswerWorkflows } from "../workflow/selection";
import type { Workflow } from "../api/workflows";

const node = (id: string, type: string): Workflow["nodes"][number] => ({
  id, type, position: { x: 0, y: 0 }, config: {},
});
const edge = (source: string, source_port: string, target: string, target_port: string): Workflow["edges"][number] => ({
  id: `${source}-${target}`, source, source_port, target, target_port,
});
const answerWorkflow: Workflow = {
  id: "answer", name: "回答", version: 1, status: "published",
  nodes: ["query", "retrieval", "gate", "llm", "citation", "output"].map((id) =>
    node(id, ({ query: "query.input", retrieval: "retrieval.dense", gate: "evidence.gate",
      llm: "generation.llm", citation: "citation.validate", output: "answer.output" } as Record<string, string>)[id]),
  ),
  edges: [
    edge("query", "question", "retrieval", "question"),
    edge("retrieval", "evidence", "gate", "evidence"),
    edge("gate", "evidence", "llm", "evidence"),
    edge("query", "question", "llm", "question"),
    edge("llm", "draft", "citation", "draft"),
    edge("gate", "evidence", "citation", "evidence"),
    edge("citation", "answer", "output", "answer"),
  ],
};
const searchWorkflow = { ...answerWorkflow, id: "search", nodes: [node("query", "query.input"), node("retrieval", "retrieval.dense"), node("output", "search.output")] };

it("recognizes a complete answer workflow", () => expect(isAnswerWorkflow(answerWorkflow)).toBe(true));
it("rejects a search-only workflow", () => expect(isAnswerWorkflow(searchWorkflow)).toBe(false));
it("filters the list without mutating it", () => {
  const workflows = [searchWorkflow, answerWorkflow];
  expect(selectAnswerWorkflows(workflows).map((item) => item.id)).toEqual(["answer"]);
  expect(workflows).toHaveLength(2);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm --prefix frontend test -- --run src/test/selection.test.ts`
Expected: FAIL because `frontend/src/workflow/selection.ts` does not exist.

- [ ] **Step 3: Write minimal implementation**

Implement graph reachability by building adjacency from `edges`, requiring the node types `query.input`, one of `retrieval.*`, `evidence.gate`, `generation.llm`, `citation.validate`, and `answer.output` to be present on the same directed reachable path. Export `selectAnswerWorkflows` as `workflows.filter(isAnswerWorkflow)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm --prefix frontend test -- --run src/test/selection.test.ts`
Expected: PASS with 3 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/workflow/selection.ts frontend/src/test/selection.test.ts
git commit -m "feat: identify selectable answer workflows"
```

### Task 2: Move workflow selection into the chat page

**Files:**
- Modify: `frontend/src/pages/ChatPage.tsx`
- Modify: `frontend/src/test/pages.test.tsx`

**Interfaces:**
- Consumes `selectAnswerWorkflows` from Task 1.
- `ChatPage` keeps `workflowId` as the ID used by `runWorkflow` and renders a visible `combobox` named `工作流` before the advanced evidence-count controls.

- [ ] **Step 1: Write failing tests**

Add tests that mock `medical-answer` and `medical-answer-expanded` plus `medical-search` and `document-ingestion`; assert only the first two appear in the chat combobox, the combobox is visible before opening advanced settings, and selecting `medical-answer-expanded` causes the next send to call `runWorkflow("medical-answer-expanded", ...)`. Add a no-answer-workflow case asserting the send button is disabled and an explanatory empty state is shown.

- [ ] **Step 2: Run the focused tests to verify the new assertions fail**

Run: `npm --prefix frontend test -- --run src/test/pages.test.tsx`
Expected: FAIL because the current selector is hidden in `高级设置` and no filtering is applied.

- [ ] **Step 3: Implement the smallest chat changes**

Import `selectAnswerWorkflows`, derive `answerWorkflows` when loading, store only that list, initialize to `medical-answer` when available or the first answer workflow otherwise, and clear the ID when no answer workflow exists. Render a labeled workflow `<select>` in a visible `settings-bar` above the `<details>`, remove the duplicate workflow select from advanced settings, disable send while workflows are loading or unavailable, and show `暂无可用的回答工作流` in the empty state. Keep `send` reading the current `workflowId` at invocation time and leave conversation storage untouched.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `npm --prefix frontend test -- --run src/test/pages.test.tsx`
Expected: PASS, including existing chat behavior and the new workflow-switching assertions.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/ChatPage.tsx frontend/src/test/pages.test.tsx
git commit -m "feat: switch workflows from chat"
```

### Task 3: Remove editor-side conversation testing

**Files:**
- Modify: `frontend/src/pages/WorkflowEditorPage.tsx`
- Modify: `frontend/src/test/editor.test.tsx`

**Interfaces:**
- Editor retains `validateWorkflow`, save, graph editing, and validation issue display; it no longer imports or calls `runWorkflow` and no longer renders `测试问题` or `运行已发布版`.

- [ ] **Step 1: Write the failing test**

Change the editor test to assert `测试问题` and `运行已发布版` are absent while `✓ 校验` remains present; keep the validation issue assertion. Run `npm --prefix frontend test -- --run src/test/editor.test.tsx` and observe failure because the current editor renders and runs the test controls.

- [ ] **Step 2: Implement the minimal removal**

Remove the `runWorkflow` import, `RunResult` type, `run` state, `testQuestion` state, `execute` function, test input/button, run trace rendering, and copy that promises to run a published version. Keep the trace panel as the validation panel and preserve node status updates only where produced by validation (no synthetic run status).

- [ ] **Step 3: Run the editor tests**

Run: `npm --prefix frontend test -- --run src/test/editor.test.tsx`
Expected: PASS with no editor test-run controls.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/WorkflowEditorPage.tsx frontend/src/test/editor.test.tsx
git commit -m "refactor: keep workflow runs in chat"
```

### Task 4: Full verification and build

**Files:**
- No new source files; inspect the complete diff and generated artifacts only.

- [ ] **Step 1: Run all frontend tests**

Run: `npm --prefix frontend test -- --run`
Expected: all Vitest suites pass with zero failures.

- [ ] **Step 2: Run typecheck, lint, and build**

Run: `npm --prefix frontend run lint` and `npm --prefix frontend run build`
Expected: TypeScript, ESLint, and Vite build exit with code 0.

- [ ] **Step 3: Run relevant backend regression tests**

Run: `pytest -q tests/test_workflow_api.py tests/test_routed_workflow_api.py tests/test_workflow_validator.py`
Expected: all selected backend tests pass, confirming retained search/ingestion and answer workflow APIs.

- [ ] **Step 4: Review requirements against the diff**

Confirm `medical-search` and `document-ingestion` JSON files remain present, `/workflows` API remains unchanged, the chat selector is visible without opening advanced settings, and no editor run control remains.

- [ ] **Step 5: Commit verification-only adjustments if needed**

If any formatting or test fixture adjustment was required, run the relevant focused test again, then commit it with `chore: verify chat workflow selection`; otherwise leave unrelated existing working-tree changes untouched.
