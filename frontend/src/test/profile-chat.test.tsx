import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ChatPage from "../pages/ChatPage";
vi.mock("../api/documents", () => ({
  getDocumentJobs: vi.fn().mockResolvedValue([]), getDocumentJob: vi.fn(),
}));
import * as chat from "../api/chat";
import * as history from "../api/conversations";
import * as memory from "../api/memory";
import type { RunResult } from "../api/workflows";
import { ApiRequestError } from "../api/client";
vi.mock("../api/workflows", () => ({
  getWorkflows: vi.fn().mockResolvedValue([]),
  getHealth: vi.fn().mockResolvedValue({ milvus: false }),
  uploadDocument: vi.fn(),
}));
vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
  confirmProfile: vi.fn(),
}));
vi.mock("../api/conversations", () => ({
  getConversations: vi.fn(),
  getConversation: vi.fn(),
  importConversations: vi.fn(),
}));
vi.mock("../api/memory", () => ({ getMemoryJob: vi.fn() }));
const result: RunResult = {
  run_id: "r1",
  status: "success",
  kind: "memory",
  output: { answer: "已处理画像" },
  trace: [],
  profile: {
    status: "processed",
    actions: [
      { key: "occupation", status: "added", message: "职业画像已新增" },
    ],
  },
  episode: { status: "skipped" },
};
beforeEach(() => {
  localStorage.clear();
  vi.mocked(history.getConversations).mockResolvedValue([]);
  vi.mocked(history.getConversation).mockImplementation(async (id) =>
    JSON.parse(localStorage.getItem("zhiyuan-conversations-v1") || "[]").find(
      (item: { id: string }) => item.id === id,
    ),
  );
  vi.mocked(chat.streamChat).mockResolvedValue(result);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
async function renderChat() {
  await act(async () => {
    render(
      <MemoryRouter>
        <ChatPage />
      </MemoryRouter>,
    );
  });
}
async function send(text = "我是一名护士") {
  fireEvent.change(screen.getByLabelText("输入医疗资料问题"), {
    target: { value: text },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "发送问题" }));
  });
}
it("allows pure memory with no workflow without claims or episode polling", async () => {
  await renderChat();
  await send();
  expect(chat.streamChat).toHaveBeenCalledWith(
    expect.objectContaining({
      request_id: expect.any(String),
      conversation_id: expect.any(String),
      workflow_id: "medical-answer",
      inputs: { question: "我是一名护士", provider: "qwen", top_k: 8 },
    }),
    expect.any(Function),
  );
  expect(screen.getByText("职业画像已新增")).toBeInTheDocument();
  expect(screen.queryByText("证据不足")).not.toBeInTheDocument();
  expect(screen.queryByText(/正在生成/)).not.toBeInTheDocument();
  expect(memory.getMemoryJob).not.toHaveBeenCalled();
});
it("retains profile feedback and application errors when the answer fails", async () => {
  vi.mocked(chat.streamChat).mockResolvedValue({
    ...result,
    status: "failed",
    kind: "mixed",
    output: {},
    error: { code: "OFFLINE", message: "检索暂不可用" },
  });
  await renderChat();
  await send();
  expect(screen.getByText("职业画像已新增")).toBeInTheDocument();
  expect(screen.getAllByText("检索暂不可用").length).toBeGreaterThan(0);
  const cached = JSON.parse(localStorage.getItem("zhiyuan-conversations-v1")!);
  expect(cached[0].messages.at(-1).profile).toEqual(result.profile);
});
it("reuses the complete original payload on explicit retry", async () => {
  vi.mocked(chat.streamChat).mockRejectedValueOnce(new Error("网络断开"));
  await renderChat();
  await send();
  const original = vi.mocked(chat.streamChat).mock.calls[0][0];
  expect(original).not.toHaveProperty("memory_consent");
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "重试此消息" }));
  });
  expect(vi.mocked(chat.streamChat).mock.calls[1][0]).toEqual(original);
  await send("我的目标是学习");
  expect(vi.mocked(chat.streamChat).mock.calls[2][0].request_id).not.toBe(
    original.request_id,
  );
  expect(
    vi.mocked(chat.streamChat).mock.calls[2][0],
  ).not.toHaveProperty("memory_consent");
});
it("restores historical profile and independent episode outcomes", async () => {
  vi.mocked(history.getConversations).mockResolvedValue([
    {
      id: "c1",
      title: "旧画像",
      messages: [
        {
          role: "assistant",
          content: "已处理画像",
          runId: "r1",
          profile: result.profile,
          episode: { status: "saved" },
        },
      ],
    },
  ]);
  await renderChat();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /旧画像/ }));
  });
  expect(await screen.findByText("职业画像已新增")).toBeInTheDocument();
  expect(screen.getByText("情景记忆已保存")).toBeInTheDocument();
});
it("uses a bound confirmation without re-running the original question", async () => {
  vi.mocked(chat.streamChat).mockResolvedValue({
    ...result,
    profile: {
      status: "processed",
      actions: [
        {
          key: "occupation",
          status: "needs_confirmation",
          message: "确认职业变更",
        },
      ],
      confirmation_token: "bound-token",
      confirmation_expires_at: Date.now() / 1000 + 300,
      confirmation_requires_consent: true,
    },
  });
  vi.mocked(chat.confirmProfile).mockResolvedValue({
    ...result,
    profile: {
      status: "processed",
      actions: [
        { key: "occupation", status: "unchanged", message: "已取消画像变更" },
      ],
    },
  });
  await renderChat();
  await send();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "取消画像变更" }));
  });
  expect(chat.confirmProfile).toHaveBeenCalledWith({
    request_id: expect.any(String),
    conversation_id: vi.mocked(chat.streamChat).mock.calls[0][0]
      .conversation_id,
    token: "bound-token",
    accept: false,
  });
  expect(chat.streamChat).toHaveBeenCalledOnce();
  await waitFor(() =>
    expect(screen.getByText("已取消画像变更")).toBeInTheDocument(),
  );
  expect(
    screen.queryByRole("button", { name: "确认画像变更" }),
  ).not.toBeInTheDocument();
});

it("shows intent progress then independent mixed feedback and prefers episode over the legacy alias", async () => {
  let finish!: (value: RunResult) => void;
  vi.mocked(chat.streamChat).mockImplementation((_payload, onEvent) => {
    onEvent?.({ event: "chat.progress", data: { stage: "intent" } });
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  await renderChat();
  await send("我是一名护士，请解释高血压");
  expect(screen.getByText("正在分析本次意图…")).toBeInTheDocument();
  await act(async () => {
    finish({
      ...result,
      kind: "mixed",
      output: { answer: "医学问题的权威回答" },
      memory: { status: "pending" },
    });
  });
  expect(screen.getByText("医学问题的权威回答")).toBeInTheDocument();
  expect(screen.getByText("职业画像已新增")).toBeInTheDocument();
  expect(memory.getMemoryJob).not.toHaveBeenCalled();
  expect(screen.queryByText(/情景记忆正在提取/)).not.toBeInTheDocument();
});

it("isolates confirmation tokens between conversations", async () => {
  const profile = {
    ...result.profile!,
    confirmation_token: "token-first",
    confirmation_expires_at: Date.now() / 1000 + 300,
    confirmation_requires_consent: true,
  };
  vi.mocked(history.getConversations).mockResolvedValue([
    {
      id: "first",
      title: "首个会话",
      messages: [{ role: "assistant", content: "画像待确认", profile }],
    },
    {
      id: "second",
      title: "另个会话",
      messages: [
        {
          role: "assistant",
          content: "画像待确认",
          profile: { ...profile, confirmation_token: "token-second" },
        },
      ],
    },
  ]);
  vi.mocked(chat.confirmProfile).mockRejectedValue(
    new ApiRequestError(
      "画像已变化，请重新提出变更",
      "MEMORY_CONFIRMATION_CONFLICT",
      409,
    ),
  );
  await renderChat();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /首个会话/ }));
  });
  expect(
    screen.getByText("此变更包含敏感信息，默认不保存；如需记录请到设置页「长期记忆」手动添加。"),
  ).toBeInTheDocument();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /另个会话/ }));
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "取消画像变更" }));
  });
  expect(chat.confirmProfile).toHaveBeenCalledWith({
    request_id: expect.any(String),
    conversation_id: "second",
    token: "token-second",
    accept: false,
  });
  expect(screen.getByText("画像已变化，请重新提出变更")).toBeInTheDocument();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /首个会话/ }));
  });
  expect(
    screen.queryByRole("button", { name: "确认画像变更" }),
  ).not.toBeInTheDocument();
});

it("retries a confirmation with the same request id after transport failure", async () => {
  vi.mocked(chat.streamChat).mockResolvedValue({
    ...result,
    profile: {
      ...result.profile!,
      confirmation_token: "retry-token",
      confirmation_expires_at: Date.now() / 1000 + 300,
    },
  });
  vi.mocked(chat.confirmProfile)
    .mockRejectedValueOnce(new Error("private transport details"))
    .mockResolvedValueOnce({
      ...result,
      profile: {
        status: "processed",
        actions: [
          { key: "occupation", status: "updated", message: "职业画像已更新" },
        ],
      },
    });
  await renderChat();
  await send();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "确认画像变更" }));
  });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "画像确认失败，请检查连接后重试",
  );
  expect(screen.queryByText(/private transport/)).not.toBeInTheDocument();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "确认画像变更" }));
  });
  expect(vi.mocked(chat.confirmProfile).mock.calls[1][0]).toEqual(
    vi.mocked(chat.confirmProfile).mock.calls[0][0],
  );
  expect(screen.getByText("职业画像已更新")).toBeInTheDocument();
});
