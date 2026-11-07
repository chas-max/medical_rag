import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { row } from "../patient/model";

vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
  confirmProfile: vi.fn(),
}));
vi.mock("../api/conversations", () => ({
  getConversations: vi.fn(),
  getConversation: vi.fn(),
  importConversations: vi.fn(),
}));
vi.mock("../api/documents", () => ({
  getDocumentJobs: vi.fn().mockResolvedValue([]),
  getDocumentJob: vi.fn(),
}));
vi.mock("../api/memory", () => ({ getMemoryJob: vi.fn() }));
vi.mock("../api/sources", () => ({ getChunk: vi.fn() }));
vi.mock("../api/workflows", () => ({
  getNodes: vi.fn(),
  getWorkflows: vi.fn(),
  getWorkflow: vi.fn(),
  getHealth: vi.fn(),
  runWorkflow: vi.fn(),
  streamWorkflow: vi.fn(),
  validateWorkflow: vi.fn(),
  saveWorkflow: vi.fn(),
  uploadDocument: vi.fn(),
}));

import App from "../App";
import * as chat from "../api/chat";
import * as historyApi from "../api/conversations";
import type { Conversation, Message } from "../api/conversations";
import * as api from "../api/workflows";
import { ApiRequestError } from "../api/client";

const workflow: api.Workflow = {
  id: "medical-answer",
  name: "医疗资料问答",
  version: 7,
  status: "published",
  nodes: [],
  edges: [],
};

const stored = (messages: Message[]): Conversation => ({
  id: "dialogue-chat",
  title: "连续追问",
  messages,
});

beforeEach(() => {
  localStorage.clear();
  vi.mocked(historyApi.getConversations).mockResolvedValue([]);
  vi.mocked(historyApi.importConversations).mockImplementation(
    async (items) => items,
  );
  vi.mocked(historyApi.getConversation).mockResolvedValue({
    id: "none",
    title: "",
    messages: [],
  });
  vi.mocked(api.getWorkflows).mockResolvedValue([workflow]);
  vi.mocked(api.getWorkflow).mockResolvedValue(workflow);
  vi.mocked(api.getNodes).mockResolvedValue([]);
  vi.mocked(api.getHealth).mockResolvedValue({ milvus: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("keeps the clarification type when history is reloaded", () => {
  const result = row(
    {
      id: "00000000-0000-0000-0000-000000000401",
      content: "你指的是高血压，还是糖尿病？",
      role: "assistant",
      citations: [],
      answer_type: "clarification",
      created_at: "2026-01-01T00:00:00Z",
    },
    "content",
  );
  expect(result.answer_type).toBe("clarification");
});

it("shows a streamed clarification as an answer, not as an evidence shortage", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "clarify-run",
    status: "success",
    output: {
      answer: "你指的是 高血压，还是 糖尿病？",
      citations: [],
      abstained: false,
      answer_type: "clarification",
    },
    trace: [],
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(screen.getByLabelText("输入医疗资料问题"), "它是什么引起的？");
  await user.click(screen.getByRole("button", { name: "发送问题" }));

  expect(
    await screen.findByText("你指的是 高血压，还是 糖尿病？"),
  ).toBeInTheDocument();
  expect(screen.queryByText("证据不足")).not.toBeInTheDocument();
  expect(screen.queryByText("生成异常")).not.toBeInTheDocument();
  // 输入框仍可继续提交，会话没有被清空成错误状态。
  expect(screen.getByLabelText("输入医疗资料问题")).toBeEnabled();
});

it("keeps the clarification badge when a stored conversation is reopened", async () => {
  const user = userEvent.setup();
  const conversation = stored([
    {
      role: "user",
      content: "它是什么引起的？",
    },
    {
      role: "assistant",
      content: "请补充具体讨论对象，并把想了解的问题说完整。",
      citations: [],
      abstained: false,
      answerType: "clarification",
    },
  ]);
  vi.mocked(historyApi.getConversations).mockResolvedValue([conversation]);
  vi.mocked(historyApi.getConversation).mockResolvedValue(conversation);
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(await screen.findByRole("button", { name: /☰ 连续追问/ }));

  expect(
    await screen.findByText("请补充具体讨论对象，并把想了解的问题说完整。"),
  ).toBeInTheDocument();
  expect(screen.getByText("需要补充对象")).toBeInTheDocument();
  expect(screen.queryByText("证据不足")).not.toBeInTheDocument();
});

it("keeps the input and the conversation after a stale-context conflict", async () => {
  const user = userEvent.setup();
  const conversation = stored([
    {
      role: "user",
      content: "已有问题",
    },
  ]);
  vi.mocked(historyApi.getConversations).mockResolvedValue([conversation]);
  vi.mocked(historyApi.getConversation).mockResolvedValue(conversation);
  vi.mocked(chat.streamChat).mockRejectedValue(
    new ApiRequestError("会话已更新，请重新发送问题", "CONVERSATION_CONTEXT_CHANGED", 409),
  );
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(await screen.findByRole("button", { name: /☰ 连续追问/ }));
  await user.type(screen.getByLabelText("输入医疗资料问题"), "新问题");
  await user.click(screen.getByRole("button", { name: "发送问题" }));

  expect(await screen.findByText("会话已更新，请重新发送问题")).toBeInTheDocument();
  // 只尝试一次：冲突不能被自动重试变成重复计费。
  expect(chat.streamChat).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: /☰ 连续追问/ })).toBeInTheDocument();
});
