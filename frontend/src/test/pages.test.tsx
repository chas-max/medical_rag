import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";
import * as documentApi from "../api/documents";
vi.mock("../api/documents", () => ({
  getDocumentJobs: vi.fn().mockResolvedValue([]), getDocumentJob: vi.fn(),
}));
import * as api from "../api/workflows";
import * as chat from "../api/chat";
vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
  confirmProfile: vi.fn(),
}));
import * as historyApi from "../api/conversations";
import * as memoryApi from "../api/memory";
import * as sources from "../api/sources";

vi.mock("../api/memory", () => ({ getMemoryJob: vi.fn() }));

vi.mock("../api/conversations", () => ({
  getConversations: vi.fn(),
  getConversation: vi.fn(),
  importConversations: vi.fn(),
}));

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

const workflow: api.Workflow = {
  id: "medical-answer",
  name: "医疗资料问答",
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
    position: { x: 0, y: 0 },
    config: {},
  })),
  edges: [
    [0, 1],
    [1, 2],
    [2, 3],
    [3, 4],
    [4, 5],
  ].map(([source, target]) => ({
    id: `edge-${source}-${target}`,
    source: `node-${source}`,
    source_port: "output",
    target: `node-${target}`,
    target_port: "input",
  })),
};
const expandedWorkflow: api.Workflow = {
  ...workflow,
  id: "medical-answer-expanded",
  name: "路由式医疗资料问答",
};
const searchWorkflow: api.Workflow = {
  ...workflow,
  id: "medical-search",
  name: "医疗资料检索",
  nodes: [
    { id: "query", type: "query.input", position: { x: 0, y: 0 }, config: {} },
    {
      id: "retrieval",
      type: "retrieval.dense",
      position: { x: 0, y: 0 },
      config: {},
    },
    {
      id: "output",
      type: "search.output",
      position: { x: 0, y: 0 },
      config: {},
    },
  ],
  edges: [
    {
      id: "query-retrieval",
      source: "query",
      source_port: "question",
      target: "retrieval",
      target_port: "question",
    },
    {
      id: "retrieval-output",
      source: "retrieval",
      source_port: "evidence",
      target: "output",
      target_port: "evidence",
    },
  ],
};
const ingestionWorkflow: api.Workflow = {
  ...workflow,
  id: "document-ingestion",
  name: "文档入库",
  nodes: [
    {
      id: "input",
      type: "document.input",
      position: { x: 0, y: 0 },
      config: {},
    },
  ],
  edges: [],
};
beforeEach(() => {
  localStorage.clear();
  vi.mocked(historyApi.getConversations).mockResolvedValue([]);
  vi.mocked(historyApi.importConversations).mockImplementation(
    async (items) => items,
  );
  vi.mocked(historyApi.getConversation).mockImplementation(async (id) => {
    const items = JSON.parse(
      localStorage.getItem("zhiyuan-conversations-v1") || "[]",
    );
    return items.find((item: { id: string }) => item.id === id);
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

it("keeps uploaded documents pending until the background job reports completion", async () => {
  const user = userEvent.setup();
  vi.mocked(api.uploadDocument).mockResolvedValue({
    job_id: "new-upload", status: "queued", status_url: "/document-jobs/new-upload",
  });
  vi.mocked(documentApi.getDocumentJob).mockResolvedValue({
    job_id: "new-upload", filename: "guide.md", source_id: "guide.md", status: "queued",
    stage: "queued", attempts: 0, result: null, error: null, trace: [],
  });
  const view = render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  const input = view.container.querySelector<HTMLInputElement>('input[type="file"]')!;
  await user.upload(input, new File(["公开资料"], "guide.md"));
  expect(await screen.findByText(/guide.md：已接收/)).toBeInTheDocument();
  expect(screen.queryByText(/已入库 .* 个文本块/)).not.toBeInTheDocument();
  expect(api.uploadDocument).toHaveBeenCalledOnce();
});

it("never sends sensitive-memory consent and separates saving from answer completion", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "memory-run",
    status: "success",
    output: { answer: "权威回答" },
    trace: [],
    memory: { status: "pending" },
  });
  vi.mocked(memoryApi.getMemoryJob).mockResolvedValue({
    run_id: "memory-run",
    status: "processing",
    error: null,
  });
  const view = render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  expect(
    screen.queryByRole("checkbox", {
      name: "仅本次允许记住我提供的敏感个人信息",
    }),
  ).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("输入医疗资料问题"), "我的自述");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByText("权威回答")).toBeInTheDocument();
  const payload = vi.mocked(chat.streamChat).mock.lastCall
    ?.[0] as unknown as Record<string, unknown>;
  expect(payload.workflow_id).toBe("medical-answer");
  expect(payload.inputs).toEqual({
    question: "我的自述",
    provider: "qwen",
    top_k: 8,
  });
  expect(payload).not.toHaveProperty("memory_consent");
  expect(await screen.findByText(/情景记忆正在提取/)).toBeInTheDocument();
  expect(screen.queryByText("情景记忆已保存")).not.toBeInTheDocument();
  view.unmount();
});

it("shows memory failure separately without discarding the successful answer", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "memory-failed",
    status: "success",
    output: { answer: "回答保留" },
    trace: [],
    memory: { status: "failed" },
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(screen.getByLabelText("输入医疗资料问题"), "普通问题");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "发送问题" })).not.toBeDisabled(),
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByText("回答保留")).toBeInTheDocument();
  expect(screen.getByText(/情景记忆保存失败/)).toBeInTheDocument();
  expect(memoryApi.getMemoryJob).not.toHaveBeenCalled();
});

it("shows container progress before claims and persists a safe final trace across chat reload", async () => {
  const user = userEvent.setup();
  let finish!: (result: api.RunResult) => void;
  vi.mocked(chat.streamChat).mockImplementation(async (_payload, emit) => {
    emit?.({
      event: "node.progress",
      data: {
        node_id: "agent",
        stage: "planning",
        iteration: 1,
        evidence: 2,
      },
    });
    return await new Promise((resolve) => {
      finish = resolve;
    });
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "问题",
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByText(/第 1 轮 · 规划补充查询/)).toBeInTheDocument();
  await act(async () =>
    finish({
      run_id: "trace-run",
      status: "success",
      output: { answer: "最终答案" },
      trace: [
        {
          node_id: "agent",
          status: "success",
          duration_ms: 11,
          error: null,
          metadata: {
            secret: "PRIVATE",
            agentic: {
              stop_reason: "max_rounds",
              retrieval_rounds: 1,
              model_calls: 2,
              iterations: [],
            },
          },
        },
      ],
    }),
  );
  expect(await screen.findByText(/运行轨迹 · trace-run/)).toBeInTheDocument();
  expect(localStorage.getItem("zhiyuan-conversations-v1")).not.toContain(
    "PRIVATE",
  );
  cleanup();
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(screen.getByRole("button", { name: /☰ 问题/ }));
  expect(screen.getByText(/运行轨迹 · trace-run/)).toBeInTheDocument();
});

it("shows the returned failed run trace without rendering an answer", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "failed-trace",
    status: "failed",
    output: {},
    trace: [
      {
        node_id: "failed-agent",
        status: "failed",
        duration_ms: 9,
        metadata: null as unknown as Record<string, unknown>,
        error: { code: "TIMEOUT", message: "修正超时" },
      },
    ],
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "失败问题",
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(
    await screen.findByText(/运行轨迹 · failed-trace/),
  ).toBeInTheDocument();
  expect(screen.getByText(/failed-agent/)).toHaveTextContent("TIMEOUT");
});

it("keeps each failed done trace after another request and reload", async () => {
  const user = userEvent.setup();
  const failed = (run_id: string): api.RunResult => ({
    run_id,
    status: "failed",
    output: {},
    trace: [
      {
        node_id: "failed-agent",
        status: "failed",
        duration_ms: 9,
        metadata: { raw_response: "PRIVATE" },
        error: { code: "TIMEOUT", message: "修正超时" },
      },
    ],
  });
  vi.mocked(chat.streamChat)
    .mockResolvedValueOnce(failed("failed-first"))
    .mockResolvedValueOnce(failed("failed-second"));
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "第一次问题",
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(
    await screen.findByText(/运行轨迹 · failed-first/),
  ).toBeInTheDocument();
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "第二次问题",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(
    await screen.findByText(/运行轨迹 · failed-second/),
  ).toBeInTheDocument();
  expect(screen.getByText(/运行轨迹 · failed-first/)).toBeInTheDocument();
  expect(localStorage.getItem("zhiyuan-conversations-v1")).not.toContain(
    "PRIVATE",
  );
  cleanup();
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(screen.getByRole("button", { name: /☰ 第一次问题/ }));
  expect(screen.getByText(/运行轨迹 · failed-first/)).toBeInTheDocument();
  expect(screen.getByText(/运行轨迹 · failed-second/)).toBeInTheDocument();
});

it("does not expose an editor page", async () => {
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  expect(
    await screen.findByRole("heading", { name: /从可信资料中寻找答案/ }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /工作流编辑器/ })).not.toBeInTheDocument();
});

it("recovers an answer completed after leaving the chat route", async () => {
  const user = userEvent.setup();
  let finish!: (result: api.RunResult) => void;
  vi.mocked(chat.streamChat).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  const view = render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "离开页面的问题",
  );
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  view.unmount();
  await act(async () =>
    finish({
      run_id: "off-route",
      status: "success",
      output: { answer: "离开后完成的答案", citations: [], abstained: false },
      trace: [],
    }),
  );
  // The server finishes and commits even though ChatPage is no longer mounted.
  const cached = JSON.parse(
    localStorage.getItem("zhiyuan-conversations-v1") || "[]",
  );
  const completed = {
    ...cached[0],
    messages: [
      ...cached[0].messages,
      {
        role: "assistant",
        content: "离开后完成的答案",
        citations: [],
        runId: "off-route",
      },
    ],
  };
  vi.mocked(historyApi.getConversations).mockResolvedValue([completed]);
  vi.mocked(historyApi.getConversation).mockResolvedValue(completed);
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.click(
    await screen.findByRole("button", { name: /☰ 离开页面的问题/ }),
  );
  expect(await screen.findByText("离开后完成的答案")).toBeInTheDocument();
});

it("loads server history without browser storage and refreshes the same selected chat", async () => {
  const user = userEvent.setup();
  const pending = {
    id: "server-chat",
    title: "服务端的问题",
    messages: [{ role: "user" as const, content: "服务端的问题" }],
  };
  vi.mocked(historyApi.getConversations).mockResolvedValue([pending]);
  vi.mocked(historyApi.getConversation).mockResolvedValue(pending);
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  const chat = await screen.findByRole("button", { name: /☰ 服务端的问题/ });
  await user.click(chat);
  await waitFor(() =>
    expect(historyApi.getConversation).toHaveBeenCalledTimes(1),
  );
  vi.mocked(historyApi.getConversation).mockResolvedValue({
    ...pending,
    messages: [
      ...pending.messages,
      { role: "assistant", content: "后台刚保存的回答" },
    ],
  });
  await user.click(chat);
  expect(await screen.findByText("后台刚保存的回答")).toBeInTheDocument();
  expect(historyApi.importConversations).not.toHaveBeenCalled();
});

it("does not replace server answers with stale browser-only questions", async () => {
  const user = userEvent.setup();
  const cached = {
    id: "same-chat",
    title: "缓存的问题",
    messages: [{ role: "user" as const, content: "缓存的问题" }],
  };
  localStorage.setItem("zhiyuan-conversations-v1", JSON.stringify([cached]));
  const saved = {
    ...cached,
    messages: [
      ...cached.messages,
      { role: "assistant" as const, content: "服务器已有的完整回答" },
    ],
  };
  vi.mocked(historyApi.getConversations).mockResolvedValue([saved]);
  vi.mocked(historyApi.getConversation).mockResolvedValue(saved);
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(
    await screen.findByRole("button", { name: /☰ 缓存的问题/ }),
  );
  expect(await screen.findByText("服务器已有的完整回答")).toBeInTheDocument();
  expect(historyApi.importConversations).not.toHaveBeenCalled();
});

it("keeps server answers readable if localStorage quota is exceeded", async () => {
  const user = userEvent.setup();
  const saved = {
    id: "quota-chat",
    title: "缓存已满的问题",
    messages: [
      { role: "user" as const, content: "缓存已满的问题" },
      { role: "assistant" as const, content: "持久化的完整答案" },
    ],
  };
  vi.mocked(historyApi.getConversations).mockResolvedValue([saved]);
  vi.mocked(historyApi.getConversation).mockResolvedValue(saved);
  const write = vi
    .spyOn(Storage.prototype, "setItem")
    .mockImplementation((key) => {
      if (key === "zhiyuan-conversations-v1")
        throw new DOMException("Full", "QuotaExceededError");
    });
  try {
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <App />
      </MemoryRouter>,
    );
    await user.click(
      await screen.findByRole("button", { name: /☰ 缓存已满的问题/ }),
    );
    expect(await screen.findByText("持久化的完整答案")).toBeInTheDocument();
    expect(screen.getByText(/浏览器历史缓存不可用/)).toBeInTheDocument();
  } finally {
    write.mockRestore();
  }
});

it("does not start a new turn before legacy history has been safely imported", async () => {
  const user = userEvent.setup();
  const legacy = {
    id: "unmigrated",
    title: "尚未迁移的旧对话",
    messages: [
      { role: "user", content: "旧问题" },
      { role: "assistant", content: "不能丢失的旧答案" },
    ],
  };
  localStorage.setItem("zhiyuan-conversations-v1", JSON.stringify([legacy]));
  vi.mocked(historyApi.importConversations).mockRejectedValue(
    new Error("Import failed"),
  );
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await screen.findByText(/历史记录同步失败/);
  await user.click(screen.getByRole("button", { name: /☰ 尚未迁移的旧对话/ }));
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "新的问题",
  );
  expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
  expect(chat.streamChat).not.toHaveBeenCalled();
  expect(screen.getByText("不能丢失的旧答案")).toBeInTheDocument();
  expect(localStorage.getItem("zhiyuan-conversations-v1")).toContain(
    "不能丢失的旧答案",
  );
});

it("ignores an old history detail response that arrives after sending a new turn", async () => {
  const user = userEvent.setup();
  const saved = {
    id: "stale-detail",
    title: "已有对话",
    messages: [
      { role: "user" as const, content: "以前的问题" },
      { role: "assistant" as const, content: "以前的答案" },
    ],
  };
  let finishDetail!: (value: typeof saved) => void;
  vi.mocked(historyApi.getConversations).mockResolvedValue([saved]);
  vi.mocked(historyApi.getConversation).mockReturnValue(
    new Promise((resolve) => {
      finishDetail = resolve;
    }),
  );
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "newer-run",
    status: "success",
    output: { answer: "新的答案" },
    trace: [],
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(await screen.findByRole("button", { name: /☰ 已有对话/ }));
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "新的问题",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await screen.findByText("新的答案");
  await act(async () => finishDetail(saved));
  expect(screen.getByText("新的答案")).toBeInTheDocument();
  expect(screen.getByText("新的问题")).toBeInTheDocument();
});

it("sends a medical question and displays verified citations", async () => {
  const user = userEvent.setup();
  vi.mocked(sources.getChunk).mockResolvedValue({
    chunk_id: "chunk-1",
    document_id: "doc-1",
    source_id: "指南.md",
    filename: "指南.md",
    section: "",
    page: 2,
    ordinal: 1,
    text: "应定期复查",
    parent_text: "父块完整上下文：应定期复查并记录结果",
    parent_ordinal: 1,
    token_count: 40,
  });
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "run-1",
    status: "success",
    output: {
      answer: "应定期复查 [chunk-1]\n复查结果需要记录 [chunk-2]",
      abstained: false,
      citations: [
        {
          chunk_id: "chunk-1",
          filename: "指南.md",
          excerpt: "应定期复查",
          page: 2,
        },
        {
          chunk_id: "chunk-2",
          filename: "指南.md",
          excerpt: "复查结果需要记录",
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
  expect(await screen.findByText("知源助手")).toBeInTheDocument();
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(
    screen.getByRole("button", { name: "查看引用 1：指南.md" }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/\[chunk-1\]/)).not.toBeInTheDocument();
  expect(screen.queryByText(/运行记录 #/)).not.toBeInTheDocument();
  expect(
    screen.queryByRole("combobox", { name: "模型" }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "来源 1：指南.md" }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "查看引用 1：指南.md" }));
  expect(screen.getByRole("heading", { name: "引用原文" })).toBeInTheDocument();
  await waitFor(() => expect(sources.getChunk).toHaveBeenCalledWith("chunk-1"));
  expect(
    await screen.findByText(/父块完整上下文：应定期复查/),
  ).toBeInTheDocument();
  await waitFor(() =>
    expect(chat.streamChat).toHaveBeenCalledWith(
      {
        request_id: expect.any(String),
        conversation_id: expect.any(String),
        workflow_id: "medical-answer",
        inputs: {
          question: "如何复查？",
          provider: "qwen",
          top_k: 8,
        },
      },
      expect.any(Function),
    ),
  );
});

it("shows failed model runs as an alert instead of an assistant answer", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "failed-1",
    status: "failed",
    output: {},
    trace: [
      {
        node_id: "llm",
        status: "failed",
        duration_ms: 1,
        metadata: {},
        error: {
          code: "MODEL_RATE_LIMITED",
          message: "生成模型请求过于频繁，请稍后重试",
        },
      },
    ],
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
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "生成模型请求过于频繁",
  );
  expect(screen.queryByText("知源助手")).not.toBeInTheDocument();
  expect(screen.queryByText("证据不足")).not.toBeInTheDocument();
});

it.each(["", "  \n  "])(
  "reports an empty successful answer (%j) without saving an assistant message",
  async (answer) => {
    const user = userEvent.setup();
    vi.mocked(chat.streamChat).mockResolvedValue({
      run_id: "empty-answer",
      status: "success",
      output: { answer, abstained: false, citations: [] },
      trace: [],
    });
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <App />
      </MemoryRouter>,
    );
    await user.type(
      screen.getByRole("textbox", { name: "输入医疗资料问题" }),
      "无答案问题",
    );
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "该工作流没有产生回答",
    );
    expect(screen.queryByText("知源助手")).not.toBeInTheDocument();
    expect(
      JSON.parse(localStorage.getItem("zhiyuan-conversations-v1") || "[]")[0]
        .messages,
    ).toEqual([{ role: "user", content: "无答案问题" }]);
  },
);

it("clears the failed-run alert when switching to an existing successful conversation", async () => {
  localStorage.setItem(
    "zhiyuan-conversations-v1",
    JSON.stringify([
      {
        id: "successful",
        title: "已完成的问题",
        messages: [
          { role: "user", content: "已完成的问题" },
          { role: "assistant", content: "已核实的回答" },
        ],
      },
    ]),
  );
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "failed-2",
    status: "failed",
    output: {},
    trace: [
      {
        node_id: "llm",
        status: "failed",
        duration_ms: 1,
        metadata: {},
        error: {
          code: "MODEL_RATE_LIMITED",
          message: "生成模型请求过于频繁，请稍后重试",
        },
      },
    ],
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "新的失败问题",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "生成模型请求过于频繁",
  );
  await user.click(screen.getByRole("button", { name: /已完成的问题/ }));
  expect(screen.getByText("已核实的回答")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(
    screen.queryByText("新的失败问题", { selector: ".message-text" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /新的失败问题/ }));
  expect(screen.getByRole("alert")).toHaveTextContent("生成模型请求过于频繁");
  await user.click(screen.getByRole("button", { name: "关闭错误提示" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /已完成的问题/ }));
  await user.click(screen.getByRole("button", { name: /新的失败问题/ }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it.each(["failed", "rejected"] as const)(
  "does not show a late %s run error in another conversation",
  async (outcome) => {
    localStorage.setItem(
      "zhiyuan-conversations-v1",
      JSON.stringify([
        {
          id: "successful",
          title: "已完成的问题",
          messages: [
            { role: "user", content: "已完成的问题" },
            { role: "assistant", content: "已核实的回答" },
          ],
        },
      ]),
    );
    let resolveRun!: (value: api.RunResult) => void;
    let rejectRun!: (reason: Error) => void;
    vi.mocked(chat.streamChat).mockReturnValue(
      new Promise<api.RunResult>((resolve, reject) => {
        resolveRun = resolve;
        rejectRun = reject;
      }),
    );
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <App />
      </MemoryRouter>,
    );
    await user.type(
      screen.getByRole("textbox", { name: "输入医疗资料问题" }),
      "等待中的问题",
    );
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(chat.streamChat).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: /已完成的问题/ }));
    expect(screen.getByText("已核实的回答")).toBeInTheDocument();
    await act(async () => {
      if (outcome === "failed") {
        resolveRun({
          run_id: "late-failure",
          status: "failed",
          output: {},
          trace: [
            {
              node_id: "llm",
              status: "failed",
              duration_ms: 1,
              metadata: {},
              error: { code: "MODEL_RATE_LIMITED", message: "迟到的失败" },
            },
          ],
        });
      } else {
        rejectRun(new Error("迟到的请求错误"));
      }
    });
    expect(screen.getByText("已核实的回答")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      screen.queryByText("等待中的问题", { selector: ".message-text" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /等待中的问题/ }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      outcome === "failed" ? "迟到的失败" : "迟到的请求错误",
    );
  },
);

it("clears the conversation's error when retrying", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValueOnce({
    run_id: "failed-then-retry",
    status: "failed",
    output: {},
    trace: [
      {
        node_id: "llm",
        status: "failed",
        duration_ms: 1,
        metadata: {},
        error: { code: "MODEL_RATE_LIMITED", message: "首次请求失败" },
      },
    ],
  });
  let resolveRetry!: (value: api.RunResult) => void;
  vi.mocked(chat.streamChat).mockImplementationOnce(
    () =>
      new Promise<api.RunResult>((resolve) => {
        resolveRetry = resolve;
      }),
  );
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "重试问题",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("首次请求失败");
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "再次尝试",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await act(async () =>
    resolveRetry({
      run_id: "retry-success",
      status: "success",
      output: { answer: "重试回答", abstained: false, citations: [] },
      trace: [],
    }),
  );
  expect(screen.getByText("重试回答")).toBeInTheDocument();
});

it("stores a late successful answer in its original conversation", async () => {
  localStorage.setItem(
    "zhiyuan-conversations-v1",
    JSON.stringify([
      {
        id: "successful",
        title: "已完成的问题",
        messages: [
          { role: "user", content: "已完成的问题" },
          { role: "assistant", content: "已核实的回答" },
        ],
      },
    ]),
  );
  let resolveRun!: (value: api.RunResult) => void;
  vi.mocked(chat.streamChat).mockReturnValue(
    new Promise<api.RunResult>((resolve) => {
      resolveRun = resolve;
    }),
  );
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "等待中的问题",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await user.click(screen.getByRole("button", { name: /已完成的问题/ }));
  await act(async () =>
    resolveRun({
      run_id: "late-success",
      status: "success",
      output: { answer: "迟到的回答", abstained: false, citations: [] },
      trace: [],
    }),
  );
  expect(screen.getByText("已核实的回答")).toBeInTheDocument();
  expect(screen.queryByText("迟到的回答")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /等待中的问题/ }));
  expect(screen.getByText("迟到的回答")).toBeInTheDocument();
});

it("keeps old local conversations readable without citation metadata", async () => {
  localStorage.setItem(
    "zhiyuan-conversations-v1",
    JSON.stringify([
      {
        id: "old",
        title: "旧问题",
        messages: [
          { role: "user", content: "旧问题" },
          {
            role: "assistant",
            content: "旧回答 [legacy]",
            citations: [{ filename: "旧资料" }],
            runId: "old-run",
          },
        ],
      },
    ]),
  );
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(screen.getByRole("button", { name: /旧问题/ }));
  expect(screen.getByText("旧回答 [legacy]")).toBeInTheDocument();
  expect(screen.queryByText(/运行记录 #/)).not.toBeInTheDocument();
});

it("keeps workflow controls collapsed until requested", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "run-2",
    status: "success",
    output: { answer: "资料不足", abstained: true, citations: [] },
    trace: [],
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  expect(
    screen.queryByRole("combobox", { name: "证据数" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByText("高级设置"));
  await user.selectOptions(
    screen.getByRole("combobox", { name: "证据数" }),
    "8",
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "高血压如何随访？",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() =>
    expect(chat.streamChat).toHaveBeenCalledWith(
      {
        request_id: expect.any(String),
        conversation_id: expect.any(String),
        workflow_id: "medical-answer",
        inputs: {
          question: "高血压如何随访？",
          provider: "qwen",
          top_k: 8,
        },
      },
      expect.any(Function),
    ),
  );
});

it("renders verified claims as separate lines with compact source markers", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "format-1",
    status: "success",
    output: {
      answer: "要点一 [block-1]\n要点二 [block-2]",
      abstained: false,
      citations: [
        { chunk_id: "block-1", filename: "指南.md", excerpt: "要点一" },
        { chunk_id: "block-2", filename: "问答.pdf", excerpt: "要点二" },
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
    "有哪些要点？",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByText(/要点一/)).toBeInTheDocument();
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(
    screen.getByRole("button", { name: "查看引用 1：指南.md" }),
  ).toBeInTheDocument();
});

it("renders each verified claim while the answer is still streaming", async () => {
  let release!: (value: api.RunResult) => void;
  vi.mocked(chat.streamChat).mockImplementation((_payload, onEvent) => {
    onEvent?.({
      event: "claim",
      data: {
        text: "第一条结论 [chunk-1]",
        citations: [
          { chunk_id: "chunk-1", filename: "指南.md", excerpt: "第一条结论" },
        ],
      },
    });
    onEvent?.({
      event: "claim",
      data: {
        text: "第二条结论 [chunk-1]",
        citations: [
          { chunk_id: "chunk-1", filename: "指南.md", excerpt: "第二条结论" },
        ],
      },
    });
    return new Promise<api.RunResult>((resolve) => {
      release = resolve;
    });
  });
  const user = userEvent.setup();
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

  // 最终结果还没到，但已校验的结论和引用已经可读、可点开。
  expect(await screen.findByText(/第一条结论/)).toBeInTheDocument();
  expect(screen.getByText(/第二条结论/)).toBeInTheDocument();
  expect(screen.getByText("已校验 · 生成中")).toBeInTheDocument();
  expect(
    screen.getAllByRole("button", { name: "查看引用 1：指南.md" }),
  ).toHaveLength(2);
  expect(
    screen.getByRole("button", { name: "来源 1：指南.md" }),
  ).toBeInTheDocument();
  // 同一条引用不会因为两条结论都引它而重复出现。
  expect(
    screen.getAllByRole("button", { name: "来源 1：指南.md" }),
  ).toHaveLength(1);

  await act(async () =>
    release({
      run_id: "stream-1",
      status: "success",
      output: {
        answer: "第一条结论 [chunk-1]\n第二条结论 [chunk-1]",
        abstained: false,
        citations: [
          { chunk_id: "chunk-1", filename: "指南.md", excerpt: "第一条结论" },
        ],
      },
      trace: [],
    }),
  );
  expect(screen.queryByText("已校验 · 生成中")).not.toBeInTheDocument();
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
});

it("renders the streamed interpretive section below the verified claims", async () => {
  vi.mocked(chat.streamChat).mockImplementation(async (_payload, onEvent) => {
    onEvent?.({
      event: "claim",
      data: {
        text: "应规律随访 [chunk-1]",
        citations: [
          { chunk_id: "chunk-1", filename: "指南.md", excerpt: "规律随访" },
        ],
      },
    });
    onEvent?.({
      event: "analysis",
      data: {
        summary: "规律随访属于长期管理的一部分，便于医生及时调整方案。",
        extensions: ["复诊时带上既往检查资料。"],
        based_on: ["chunk-1"],
      },
    });
    return {
      run_id: "stream-analysis",
      status: "success",
      output: {
        answer: "应规律随访 [chunk-1]",
        abstained: false,
        citations: [
          { chunk_id: "chunk-1", filename: "指南.md", excerpt: "规律随访" },
        ],
        analysis: {
          summary: "规律随访属于长期管理的一部分，便于医生及时调整方案。",
          extensions: ["复诊时带上既往检查资料。"],
          based_on: ["chunk-1"],
        },
      },
      trace: [],
    };
  });
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "如何随访？",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));

  // 结论仍然是一条列表项，解释段单独成块并标明不是事实来源。
  expect(await screen.findByText(/应规律随访/)).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "延伸解读" })).toBeInTheDocument();
  expect(screen.getByText("延伸解读 · 非事实来源")).toBeInTheDocument();
  expect(screen.getByText(/规律随访属于长期管理的一部分/)).toBeInTheDocument();
  expect(screen.getByText("复诊时带上既往检查资料。")).toBeInTheDocument();
});

it("drops a partial streamed answer when the run fails or the stream breaks", async () => {
  const partial = {
    event: "claim" as const,
    data: {
      text: "半截结论 [chunk-1]",
      citations: [
        { chunk_id: "chunk-1", filename: "指南.md", excerpt: "半截结论" },
      ],
    },
  };
  const failed: api.RunResult = {
    run_id: "late-failure",
    status: "failed",
    output: {},
    trace: [
      {
        node_id: "llm",
        status: "failed",
        duration_ms: 1,
        metadata: {},
        error: { code: "MODEL_RATE_LIMITED", message: "生成模型请求过于频繁" },
      },
    ],
  };
  vi.mocked(chat.streamChat)
    .mockImplementationOnce(async (_payload, onEvent) => {
      onEvent?.(partial);
      return failed;
    })
    .mockImplementationOnce(async (_payload, onEvent) => {
      onEvent?.(partial);
      throw new Error("流式响应意外中断，请重试");
    });
  const user = userEvent.setup();
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
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "生成模型请求过于频繁",
  );
  expect(screen.queryByText(/半截结论/)).not.toBeInTheDocument();
  expect(screen.queryByText("知源助手")).not.toBeInTheDocument();

  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "再次复查？",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "流式响应意外中断，请重试",
  );
  expect(screen.queryByText(/半截结论/)).not.toBeInTheDocument();
});

it("shows retrieval progress while the answer is still being generated", async () => {
  let release!: (value: api.RunResult) => void;
  vi.mocked(chat.streamChat).mockImplementation((_payload, onEvent) => {
    onEvent?.({
      event: "status",
      data: { stage: "generating", evidence: 6, web_search: true },
    });
    return new Promise<api.RunResult>((resolve) => {
      release = resolve;
    });
  });
  const user = userEvent.setup();
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

  // 生成阶段可能持续几十秒，界面必须说明已检索到多少证据，而不是停在“正在检索”。
  expect(
    await screen.findByText(
      /已检索到 6 条证据，正在生成回答（必要时联网补充）…/,
    ),
  ).toBeInTheDocument();

  await act(async () =>
    release({
      run_id: "status-1",
      status: "success",
      output: {
        answer: "应定期复查 [chunk-1]",
        abstained: false,
        citations: [
          { chunk_id: "chunk-1", filename: "指南.md", excerpt: "应定期复查" },
        ],
      },
      trace: [],
    }),
  );
  expect(screen.queryByText(/正在生成回答/)).not.toBeInTheDocument();
});

it("offers no workflow switcher on the chat page", async () => {
  vi.mocked(api.getWorkflows).mockResolvedValue([
    searchWorkflow,
    ingestionWorkflow,
    expandedWorkflow,
    workflow,
  ]);
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await screen.findByRole("textbox", { name: "输入医疗资料问题" });
  expect(api.getWorkflows).not.toHaveBeenCalled();
  expect(
    screen.queryByRole("combobox", { name: "工作流" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText(/医疗资料问答 · v/)).not.toBeInTheDocument();
  expect(screen.queryByText(/暂无可用的回答工作流/)).not.toBeInTheDocument();
});

it("ignores a previously selected editor workflow", async () => {
  const user = userEvent.setup();
  localStorage.setItem(
    "zhiyuan-current-workflow-v1",
    "medical-answer-expanded",
  );
  vi.mocked(api.getWorkflows).mockResolvedValue([workflow, expandedWorkflow]);
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "expanded-run",
    status: "success",
    output: { answer: "路由回答", abstained: false, citations: [] },
    trace: [],
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "需要路由的问题",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() =>
    expect(chat.streamChat).toHaveBeenCalledWith(
      {
        request_id: expect.any(String),
        conversation_id: expect.any(String),
        workflow_id: "medical-answer",
        inputs: {
          question: "需要路由的问题",
          provider: "qwen",
          top_k: 8,
        },
      },
      expect.any(Function),
    ),
  );
});

it("does not expose an editor link in advanced settings", async () => {
  const user = userEvent.setup();
  localStorage.setItem(
    "zhiyuan-current-workflow-v1",
    "medical-answer-expanded",
  );
  vi.mocked(api.getWorkflows).mockResolvedValue([workflow, expandedWorkflow]);
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(screen.getByText("高级设置"));
  expect(screen.queryByRole("link", { name: /切换 \/ 编辑工作流/ })).not.toBeInTheDocument();
});

it("drops a stale editor workflow id and falls back to the default answer flow", async () => {
  const user = userEvent.setup();
  localStorage.setItem("zhiyuan-current-workflow-v1", "unpublished-draft");
  vi.mocked(api.getWorkflows).mockResolvedValue([workflow, expandedWorkflow]);
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "fallback-run",
    status: "success",
    output: { answer: "默认回答", abstained: false, citations: [] },
    trace: [],
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "默认流程问题",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() =>
    expect(chat.streamChat).toHaveBeenCalledWith(
      {
        request_id: expect.any(String),
        conversation_id: expect.any(String),
        workflow_id: "medical-answer",
        inputs: { question: "默认流程问题", provider: "qwen", top_k: 8 },
      },
      expect.any(Function),
    ),
  );
  expect(localStorage.getItem("zhiyuan-current-workflow-v1")).toBe("unpublished-draft");
});

it("allows profile management when no complete answer workflow is available", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "memory-only",
    status: "success",
    kind: "memory",
    output: { answer: "职业画像已新增" },
    trace: [],
    episode: { status: "skipped" },
  });
  vi.mocked(api.getWorkflows).mockResolvedValue([
    searchWorkflow,
    ingestionWorkflow,
  ]);
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  expect(screen.queryByText(/暂无可用的回答工作流/)).not.toBeInTheDocument();
  expect(chat.streamChat).not.toHaveBeenCalled();
  await user.type(screen.getByLabelText("输入医疗资料问题"), "我是一名护士");
  expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByText("职业画像已新增")).toBeInTheDocument();
  expect(chat.streamChat).toHaveBeenCalledWith(
    {
      request_id: expect.any(String),
      conversation_id: expect.any(String),
      workflow_id: "medical-answer",
      inputs: { question: "我是一名护士", provider: "qwen", top_k: 8 },
    },
    expect.any(Function),
  );
});

it("labels a generation fault as a generation fault instead of missing evidence", async () => {
  const user = userEvent.setup();
  vi.mocked(chat.streamChat).mockResolvedValue({
    run_id: "parse-error-run",
    status: "success",
    output: {
      answer:
        "回答生成异常：模型返回的内容不是可校验的格式，本次没有生成答案。",
      abstained: true,
      abstain_reason: "ABSTAIN_PARSE_ERROR",
      citations: [],
    },
    trace: [],
  });
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.type(screen.getByLabelText("输入医疗资料问题"), "高血压如何随访？");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "发送问题" })).not.toBeDisabled(),
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(
    await screen.findByText(
      "回答生成异常：模型返回的内容不是可校验的格式，本次没有生成答案。",
    ),
  ).toBeInTheDocument();
  // 正文已经说明是格式异常，徽标不能继续说“证据不足”。
  expect(screen.getByText("生成异常")).toBeInTheDocument();
  expect(screen.queryByText("证据不足")).not.toBeInTheDocument();
});

it("keeps a saved generation-fault label after reopening the conversation", async () => {
  localStorage.setItem(
    "zhiyuan-conversations-v1",
    JSON.stringify([
      {
        id: "saved-fault",
        title: "已保存的解析失败",
        messages: [
          { role: "user", content: "旧问题" },
          {
            role: "assistant",
            content:
              "回答生成异常：模型返回的内容不是可校验的格式，本次没有生成答案。",
            abstained: true,
            abstainReason: "ABSTAIN_PARSE_ERROR",
            citations: [],
            runId: "saved-run",
          },
        ],
      },
    ]),
  );
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.click(
    screen.getByRole("button", { name: /已保存的解析失败/ }),
  );
  expect(screen.getByText("生成异常")).toBeInTheDocument();
  expect(screen.queryByText("证据不足")).not.toBeInTheDocument();
});
