import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ChatPage from "../pages/ChatPage";
vi.mock("../api/documents", () => ({
  getDocumentJobs: vi.fn().mockResolvedValue([]), getDocumentJob: vi.fn(),
}));
import * as api from "../api/workflows";
import * as chat from "../api/chat";
vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
  confirmProfile: vi.fn(),
}));
import * as memory from "../api/memory";

vi.mock("../api/conversations", () => ({
  getConversations: vi.fn().mockResolvedValue([]),
  getConversation: vi.fn(),
  importConversations: vi.fn(),
}));
vi.mock("../api/workflows", () => ({
  getWorkflows: vi.fn(),
  getHealth: vi.fn().mockResolvedValue({ milvus: true }),
  streamWorkflow: vi.fn(),
  uploadDocument: vi.fn(),
}));
vi.mock("../api/memory", () => ({ getMemoryJob: vi.fn() }));

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  vi.mocked(api.getWorkflows).mockResolvedValue([
    {
      id: "medical-answer",
      name: "问答",
      version: 1,
      status: "published",
      nodes: [
        "query.input",
        "retrieval.dense",
        "evidence.gate",
        "generation.llm",
        "citation.validate",
        "answer.output",
      ].map((type, index) => ({
        id: `node-${index}`,
        type,
        config: {},
        position: { x: 0, y: 0 },
      })),
      edges: [0, 1, 2, 3, 4].map((index) => ({
        id: `edge-${index}`,
        source: `node-${index}`,
        target: `node-${index + 1}`,
        source_port: "output",
        target_port: "input",
      })),
    },
  ]);
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "run-1",
    status: "success",
    output: { answer: "回答已保存" },
    trace: [],
    memory: { status: "pending" },
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

async function answer() {
  await act(async () => {
    render(
      <MemoryRouter>
        <ChatPage />
      </MemoryRouter>,
    );
  });
  fireEvent.change(screen.getByLabelText("输入医疗资料问题"), {
    target: { value: "随访问题" },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  });
  expect(screen.getByText("回答已保存")).toBeInTheDocument();
}

it("polls pending memory and only reports saved after the server confirms", async () => {
  vi.mocked(memory.getMemoryJob).mockResolvedValue({
    run_id: "run-1",
    status: "saved",
    error: null,
  });
  await answer();
  expect(screen.queryByText("情景记忆已保存")).not.toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  expect(screen.getByText("情景记忆已保存")).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(memory.getMemoryJob).toHaveBeenCalledOnce();
});

it("stops after a finite polling budget without falsely reporting saved", async () => {
  vi.mocked(memory.getMemoryJob).mockResolvedValue({
    run_id: "run-1",
    status: "processing",
    error: null,
  });
  await answer();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60000);
  });
  expect(memory.getMemoryJob).toHaveBeenCalledTimes(40);
  expect(screen.getByText(/记忆仍在后台处理/)).toBeInTheDocument();
  expect(screen.queryByText("情景记忆已保存")).not.toBeInTheDocument();
});

it("cancels polling when changing conversations and ignores the old response", async () => {
  let resolve!: (job: memory.MemoryJob) => void;
  vi.mocked(memory.getMemoryJob).mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  await answer();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  const signal = vi.mocked(memory.getMemoryJob).mock.calls[0][1];
  fireEvent.click(screen.getByRole("button", { name: /新建对话/ }));
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    resolve({ run_id: "run-1", status: "saved", error: null });
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60000);
  });
  expect(memory.getMemoryJob).toHaveBeenCalledOnce();
  expect(screen.queryByText("情景记忆已保存")).not.toBeInTheDocument();
});

it("reports an unavailable memory status independently from the answer", async () => {
  vi.mocked(memory.getMemoryJob).mockRejectedValue(new Error("暂不可用"));
  await answer();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  expect(screen.getByText(/记忆状态暂不可用/)).toBeInTheDocument();
  expect(screen.getByText("回答已保存")).toBeInTheDocument();
});
