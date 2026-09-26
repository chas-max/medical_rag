import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

const workflow: api.Workflow = {
  id: "medical-answer",
  name: "医疗资料问答",
  version: 1,
  status: "published",
  nodes: [],
  edges: [],
};
beforeEach(() => {
  vi.mocked(api.getWorkflows).mockResolvedValue([workflow]);
  vi.mocked(api.getWorkflow).mockResolvedValue(workflow);
  vi.mocked(api.getNodes).mockResolvedValue([]);
  vi.mocked(api.getHealth).mockResolvedValue({ milvus: true });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("navigates between the conversation and editor pages", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  expect(
    await screen.findByRole("heading", { name: /从可信资料中寻找答案/ }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("link", { name: /工作流编辑器/ }));
  expect(await screen.findByText("节点库")).toBeInTheDocument();
});

it("sends a medical question and displays verified citations", async () => {
  const user = userEvent.setup();
  vi.mocked(api.runWorkflow).mockResolvedValue({
    run_id: "run-1",
    status: "success",
    output: {
      answer: "应定期复查 [chunk-1]",
      abstained: false,
      citations: [
        {
          chunk_id: "chunk-1",
          filename: "指南.md",
          excerpt: "应定期复查",
          page: 2,
        },
      ],
    },
    trace: [],
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "如何复查？",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByText(/应定期复查 \[chunk-1\]/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /指南.md/ })).toBeInTheDocument();
  await waitFor(() =>
    expect(api.runWorkflow).toHaveBeenCalledWith("medical-answer", {
      question: "如何复查？",
      provider: "deepseek",
      top_k: 5,
    }),
  );
});
