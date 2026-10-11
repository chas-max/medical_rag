import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api/workflows";

vi.mock("../api/workflows", () => ({
  getNodes: vi.fn(),
  getWorkflows: vi.fn(),
  getWorkflow: vi.fn(),
  getHealth: vi.fn(),
  runWorkflow: vi.fn(),
  validateWorkflow: vi.fn(),
  saveWorkflow: vi.fn(),
  uploadDocument: vi.fn(),
}));
const fitView = vi.fn();
const setViewport = vi.fn();
vi.mock("@xyflow/react", () => ({
  ReactFlowProvider: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
  useReactFlow: () => ({
    fitView,
    setViewport,
    screenToFlowPosition: ({ x, y }: { x: number; y: number }) => ({ x, y }),
  }),
  ReactFlow: ({
    nodes,
    onNodeClick,
    children,
  }: {
    nodes: { id: string; data: { metadata?: { title: string } } }[];
    onNodeClick: (event: unknown, node: unknown) => void;
    children: React.ReactNode;
  }) => (
    <div data-testid="canvas">
      {nodes.map((node) => (
        <button key={node.id} onClick={() => onNodeClick({}, node)}>
          {node.data.metadata?.title || node.id}
        </button>
      ))}
      {children}
    </div>
  ),
  Background: () => null,
  Controls: () => null,
  MiniMap: () => null,
  addEdge: (connection: unknown, edges: unknown[]) => [...edges, connection],
  applyNodeChanges: (_changes: unknown[], nodes: unknown[]) => nodes,
  applyEdgeChanges: (_changes: unknown[], edges: unknown[]) => edges,
}));

const workflow: api.Workflow = {
  id: "medical-answer",
  name: "医疗资料问答",
  version: 1,
  status: "published",
  nodes: [
    {
      id: "query",
      type: "query.input",
      position: { x: 10, y: 20 },
      config: {},
    },
  ],
  edges: [],
};
const answerNode = (
  id: string,
  type: string,
): api.Workflow["nodes"][number] => ({
  id,
  type,
  position: { x: 0, y: 0 },
  config: {},
});

const answerEdge = (source: string, target: string): api.Workflow["edges"][number] => ({
  id: `${source}-${target}`,
  source,
  source_port: "output",
  target,
  target_port: "input",
});

const answerWorkflow: api.Workflow = {
  ...workflow,
  nodes: [
    answerNode("query", "query.input"),
    answerNode("dense", "retrieval.dense"),
    answerNode("gate", "evidence.gate"),
    answerNode("llm", "generation.llm"),
    answerNode("citation", "citation.validate"),
    answerNode("output", "answer.output"),
  ],
  edges: [
    answerEdge("query", "dense"),
    answerEdge("dense", "gate"),
    answerEdge("gate", "llm"),
    answerEdge("llm", "citation"),
    answerEdge("citation", "output"),
  ],
};
const expandedWorkflow: api.Workflow = {
  ...answerWorkflow,
  id: "medical-answer-expanded",
  name: "路由式医疗资料问答",
};
// 路由条件写在节点实例上，所以这里只需要一个可被选中的 rewrite 节点。
const routedWorkflow: api.Workflow = {
  id: "medical-answer-routed",
  name: "路由试验工作流",
  version: 1,
  status: "draft",
  nodes: [answerNode("query", "query.input"), answerNode("rewrite", "question.rewrite")],
  edges: [answerEdge("query", "rewrite")],
};
const searchWorkflow: api.Workflow = {
  ...answerWorkflow,
  id: "medical-search",
  name: "医疗资料检索",
  nodes: [
    answerNode("query", "query.input"),
    answerNode("dense", "retrieval.dense"),
    answerNode("output", "search.output"),
  ],
  edges: [answerEdge("query", "dense"), answerEdge("dense", "output")],
};
const ingestionWorkflow: api.Workflow = {
  ...answerWorkflow,
  id: "document-ingestion",
  name: "文档入库",
  nodes: [answerNode("input", "document.input")],
  edges: [],
};
const port = (dtype: string): api.PortInfo => ({
  dtype,
  optional: false,
  collect: false,
  requires_tag: null,
});
const catalog: api.NodeMetadata[] = [
  {
    type: "query.input",
    title: "问题输入",
    category: "input",
    inputs: {},
    outputs: { question: "string" },
    config_schema: {},
    fixed: true,
    canonical: true,
  },
  {
    type: "retrieval.dense",
    title: "向量检索",
    category: "retrieval",
    inputs: { question: port("string") },
    outputs: { evidence: "evidence_list" },
    config_schema: { top_k: { type: "integer", default: 20, minimum: 1 } },
  },
  {
    type: "audit.record",
    title: "运行留痕",
    category: "audit",
    inputs: { question: port("string") },
    outputs: { recorded: "boolean" },
    config_schema: {},
  },
  {
    type: "question.rewrite",
    title: "问题重写",
    category: "question",
    inputs: {
      question: port("string"),
      provider: { ...port("string"), optional: true },
      route: { ...port("selector"), optional: true },
    },
    outputs: { query: "string" },
    config_schema: {},
  },
  {
    type: "evidence.gate",
    title: "证据门控",
    category: "evidence",
    inputs: { evidence: port("evidence_list") },
    outputs: { evidence: "evidence_list" },
    config_schema: {},
    tags: ["evidence.gated"],
  },
  {
    type: "generation.llm",
    title: "模型回答",
    category: "generation",
    inputs: {
      evidence: { ...port("evidence_list"), requires_tag: "evidence.gated" },
    },
    outputs: { draft: "string" },
    config_schema: {},
  },
  {
    type: "citation.validate",
    title: "引用校验",
    category: "citation",
    inputs: { draft: port("string") },
    outputs: { answer: "answer" },
    config_schema: {},
    tags: ["answer.cited"],
  },
  {
    type: "answer.output",
    title: "答案输出",
    category: "output",
    inputs: {
      answer: { ...port("answer"), requires_tag: "answer.cited" },
    },
    outputs: { answer: "string" },
    config_schema: {},
    fixed: true,
    canonical: true,
  },
  {
    type: "search.output",
    title: "证据输出",
    category: "output",
    inputs: { evidence: port("evidence_list") },
    outputs: { evidence: "evidence_list" },
    config_schema: {},
    fixed: true,
  },
];
beforeEach(() => {
  localStorage.clear();
  vi.mocked(api.getWorkflow).mockResolvedValue(workflow);
  vi.mocked(api.getWorkflows).mockResolvedValue([workflow]);
  vi.mocked(api.getNodes).mockResolvedValue(catalog);
  vi.mocked(api.saveWorkflow).mockImplementation(async (item) => item);
  vi.mocked(api.validateWorkflow).mockResolvedValue({
    valid: true,
    issues: [],
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("adds, configures and saves a new node with a version increment", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(
    await screen.findByRole("button", { name: /向量检索 retrieval.dense/ }),
  );
  expect(screen.getByRole("heading", { name: "向量检索" })).toBeInTheDocument();
  const config = screen.getByDisplayValue("20");
  await user.clear(config);
  await user.type(config, "8");
  await user.click(screen.getByRole("button", { name: "保存草稿" }));
  expect(api.saveWorkflow).toHaveBeenCalledWith(
    expect.objectContaining({
      version: 2,
      status: "draft",
      nodes: expect.arrayContaining([
        expect.objectContaining({
          type: "retrieval.dense",
          config: { top_k: 8 },
        }),
      ]),
    }),
  );
});

it("groups the palette by the node catalog so new node types are addable", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );

  // 问题处理类节点（含 merge.collect）以前被硬编码分组漏掉，画布上加不出来。
  const canvas = await screen.findByTestId("canvas");
  expect(await screen.findByRole("heading", { name: "问题处理" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /问题重写/ })).toBeInTheDocument();
  // 目录里出现的任何分类都会成组，未知分类直接用原始键名当标题。
  expect(screen.getByRole("heading", { name: "audit" })).toBeInTheDocument();

  const before = within(canvas).getAllByRole("button").length;
  await user.click(screen.getByRole("button", { name: /问题重写/ }));
  expect(within(canvas).getAllByRole("button")).toHaveLength(before + 1);
});

it("saves a routing condition on the node instance so the router stays optional", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getWorkflow).mockResolvedValue(routedWorkflow);
  vi.mocked(api.getWorkflows).mockResolvedValue([routedWorkflow]);
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer-routed"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(await screen.findByRole("button", { name: "问题重写" }));
  await user.selectOptions(screen.getByLabelText("端口"), ["route"]);
  const expected = screen.getByLabelText("期望值");
  await user.clear(expected);
  await user.type(expected, "rewrite");
  await user.click(screen.getByRole("button", { name: "保存草稿" }));

  expect(api.saveWorkflow).toHaveBeenCalledWith(
    expect.objectContaining({
      nodes: expect.arrayContaining([
        expect.objectContaining({
          type: "question.rewrite",
          activation: { port: "route", equals: "rewrite" },
        }),
      ]),
    }),
  );

  // 清掉条件后节点回到无条件执行，图里不再有任何路由依赖。
  await user.selectOptions(screen.getByLabelText("端口"), [""]);
  await user.click(screen.getByRole("button", { name: "保存草稿" }));
  const saved = vi.mocked(api.saveWorkflow).mock.calls.at(-1)?.[0];
  expect(
    saved?.nodes.find((node) => node.type === "question.rewrite")?.activation,
  ).toBeNull();
});

it("keeps validation in the editor without a conversation test runner", async () => {
  const user = userEvent.setup();
  vi.mocked(api.validateWorkflow).mockResolvedValue({
    valid: false,
    issues: [{ code: "MISSING_INPUT", message: "缺少连接", node_id: "query" }],
  });
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  expect(screen.getByRole("button", { name: "✓ 校验" })).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "测试问题" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /运行已发布版/ })).not.toBeInTheDocument();
  await user.click(await screen.findByRole("button", { name: "✓ 校验" }));
  expect(await screen.findByText(/发现 1 个校验问题/)).toBeInTheDocument();
  expect(api.runWorkflow).not.toHaveBeenCalled();
});

it("lists only complete answer workflows in the switcher", async () => {
  vi.mocked(api.getWorkflows).mockResolvedValue([
    answerWorkflow,
    expandedWorkflow,
    searchWorkflow,
    ingestionWorkflow,
  ]);
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  const selector = await screen.findByRole("combobox", { name: "切换工作流" });
  expect(
    within(selector).getByRole("option", { name: "医疗资料问答" }),
  ).toBeInTheDocument();
  expect(
    within(selector).getByRole("option", { name: "路由式医疗资料问答" }),
  ).toBeInTheDocument();
  expect(
    within(selector).queryByRole("option", { name: "医疗资料检索" }),
  ).not.toBeInTheDocument();
  expect(
    within(selector).queryByRole("option", { name: "文档入库" }),
  ).not.toBeInTheDocument();
});

it("keeps a deep-linked non-answer workflow editable", async () => {
  vi.mocked(api.getWorkflows).mockResolvedValue([answerWorkflow, searchWorkflow]);
  vi.mocked(api.getWorkflow).mockResolvedValue(searchWorkflow);
  render(
    <MemoryRouter initialEntries={["/workflows/medical-search"]}>
      <App />
    </MemoryRouter>,
  );
  expect(
    await screen.findByRole("heading", { name: "医疗资料检索" }),
  ).toBeInTheDocument();
  expect(
    await screen.findByRole("combobox", { name: "切换工作流" }),
  ).toHaveValue("medical-search");
});

it("publishes the workflow switch to the chat page only after confirming", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getWorkflows).mockResolvedValue([
    answerWorkflow,
    expandedWorkflow,
  ]);
  vi.mocked(api.getWorkflow).mockResolvedValue(answerWorkflow);
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  const selector = await screen.findByRole("combobox", { name: "切换工作流" });

  await user.selectOptions(selector, "medical-answer-expanded");
  const dialog = await screen.findByRole("dialog", {
    name: "切换到「路由式医疗资料问答 · v1」？",
  });
  expect(
    within(dialog).getByText(
      "问答页会立刻改用该工作流回答，问答页本身不再提供切换。",
    ),
  ).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(localStorage.getItem("zhiyuan-current-workflow-v1")).toBeNull();
  expect(selector).toHaveValue("medical-answer");

  await user.selectOptions(selector, "medical-answer-expanded");
  await user.click(await screen.findByRole("button", { name: "确认切换" }));
  expect(localStorage.getItem("zhiyuan-current-workflow-v1")).toBe(
    "medical-answer-expanded",
  );
});

it("offers to save canvas edits before switching workflow", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getWorkflows).mockResolvedValue([
    answerWorkflow,
    expandedWorkflow,
  ]);
  vi.mocked(api.getWorkflow).mockResolvedValue(answerWorkflow);
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(
    await screen.findByRole("button", { name: /向量检索 retrieval.dense/ }),
  );
  await user.selectOptions(
    screen.getByRole("combobox", { name: "切换工作流" }),
    "medical-answer-expanded",
  );
  expect(
    await screen.findByText("当前画布有未保存修改，切换后这些修改会丢失。"),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "先保存草稿" }));
  await waitFor(() =>
    expect(api.saveWorkflow).toHaveBeenCalledWith(
      expect.objectContaining({ status: "draft" }),
    ),
  );
  expect(localStorage.getItem("zhiyuan-current-workflow-v1")).toBeNull();
});

it("keeps fixed node types resident in the palette and undeletable on the canvas", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getWorkflows).mockResolvedValue([answerWorkflow]);
  vi.mocked(api.getWorkflow).mockResolvedValue(answerWorkflow);
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  expect(
    await screen.findByRole("button", { name: /问题输入 query.input 常驻/ }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: /答案输出 answer.output 常驻/ }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: /向量检索 retrieval.dense/ }),
  ).toBeEnabled();

  await user.click(screen.getByRole("button", { name: "问题输入" }));
  expect(screen.getByText(/固定输入节点/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "⌫ 删除节点" })).toBeDisabled();
});

it("restores a missing fixed node when the stored workflow lacks one", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getWorkflow).mockResolvedValue(workflow);
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  expect(
    await screen.findByText("已补回固定节点：答案输出"),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "答案输出" }));
  expect(screen.getByRole("button", { name: "⌫ 删除节点" })).toBeDisabled();
});

it("confirms before deleting a fixed node that has a same-category sibling", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getWorkflows).mockResolvedValue([answerWorkflow]);
  vi.mocked(api.getWorkflow).mockResolvedValue({
    ...answerWorkflow,
    nodes: [...answerWorkflow.nodes, answerNode("extra", "answer.output")],
  });
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  const outputs = await screen.findAllByRole("button", { name: "答案输出" });
  await user.click(outputs[1]);
  await user.click(screen.getByRole("button", { name: "⌫ 删除节点" }));
  const dialog = await screen.findByRole("dialog", {
    name: "删除固定输出节点「答案输出」？",
  });
  await user.click(within(dialog).getByRole("button", { name: "仍然删除" }));
  // 常驻的规范输出还在，多余的这一条被移除。
  expect(screen.getAllByRole("button", { name: "答案输出" })).toHaveLength(1);
});
