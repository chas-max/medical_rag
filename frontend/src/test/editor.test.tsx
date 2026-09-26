import { cleanup, render, screen } from "@testing-library/react";
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

it("shows validation issues and failed run traces without exposing a draft as executable", async () => {
  const user = userEvent.setup();
  vi.mocked(api.validateWorkflow).mockResolvedValue({
    valid: false,
    issues: [{ code: "MISSING_INPUT", message: "缺少连接", node_id: "query" }],
  });
  vi.mocked(api.runWorkflow).mockResolvedValue({
    run_id: "abc12345",
    status: "failed",
    output: {},
    trace: [
      {
        node_id: "query",
        status: "failed",
        duration_ms: 12,
        metadata: {},
        error: { code: "MODEL_ERROR", message: "模型服务不可用" },
      },
    ],
  });
  render(
    <MemoryRouter initialEntries={["/workflows/medical-answer"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(await screen.findByRole("button", { name: "✓ 校验" }));
  expect(await screen.findByText(/发现 1 个校验问题/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /运行已发布版/ }));
  expect(await screen.findByText("模型服务不可用")).toBeInTheDocument();
  expect(api.runWorkflow).toHaveBeenCalledWith(
    "medical-answer",
    expect.objectContaining({ question: "高血压患者如何管理？" }),
  );
});
