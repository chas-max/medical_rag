import { cleanup, render, screen, within } from "@testing-library/react";
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
const catalog: api.NodeMetadata[] = [
  {
    type: "query.input",
    title: "问题输入",
    category: "input",
    inputs: {},
    outputs: { question: "string" },
    config_schema: {},
  },
  {
    type: "retrieval.dense",
    title: "向量检索",
    category: "retrieval",
    inputs: { question: "string" },
    outputs: { evidence: "evidence_list" },
    config_schema: { top_k: { type: "integer", default: 20, minimum: 1 } },
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

it("publishes the workflow switch to the chat page", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getWorkflows).mockResolvedValue([
    answerWorkflow,
    expandedWorkflow,
  ]);
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  await user.selectOptions(
    await screen.findByRole("combobox", { name: "切换工作流" }),
    "medical-answer-expanded",
  );
  expect(localStorage.getItem("zhiyuan-current-workflow-v1")).toBe(
    "medical-answer-expanded",
  );
});
