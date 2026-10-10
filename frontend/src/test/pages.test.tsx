import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
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
    { id: "retrieval", type: "retrieval.dense", position: { x: 0, y: 0 }, config: {} },
    { id: "output", type: "search.output", position: { x: 0, y: 0 }, config: {} },
  ],
  edges: [
    { id: "query-retrieval", source: "query", source_port: "question", target: "retrieval", target_port: "question" },
    { id: "retrieval-output", source: "retrieval", source_port: "evidence", target: "output", target_port: "evidence" },
  ],
};
const ingestionWorkflow: api.Workflow = {
  ...workflow,
  id: "document-ingestion",
  name: "文档入库",
  nodes: [{ id: "input", type: "document.input", position: { x: 0, y: 0 }, config: {} }],
  edges: [],
};
beforeEach(() => {
  localStorage.clear();
  vi.mocked(api.getWorkflows).mockResolvedValue([workflow]);
  vi.mocked(api.getWorkflow).mockResolvedValue(workflow);
  vi.mocked(api.getNodes).mockResolvedValue([]);
  vi.mocked(api.getHealth).mockResolvedValue({ milvus: true });
  // 多数用例只关心最终结果：把流式入口折叠成一次 done 事件，等价于旧的整段回答。
  vi.mocked(api.streamWorkflow).mockImplementation(
    async (id, inputs, onEvent) => {
      const result = await vi.mocked(api.runWorkflow)(id, inputs);
      onEvent?.({ event: "done", data: result });
      return result;
    },
  );
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
      answer: "应定期复查 [chunk-1]\n复查结果需要记录 [chunk-2]",
      abstained: false,
      citations: [
        {
          chunk_id: "chunk-1",
          filename: "指南.md",
          excerpt: "应定期复查",
          page: 2,
        },
        { chunk_id: "chunk-2", filename: "指南.md", excerpt: "复查结果需要记录", page: 2 },
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
  expect(screen.getByRole("button", { name: "查看引用 1：指南.md" })).toBeInTheDocument();
  expect(screen.queryByText(/\[chunk-1\]/)).not.toBeInTheDocument();
  expect(screen.queryByText(/运行记录 #/)).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "模型" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "来源 1：指南.md" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "查看引用 1：指南.md" }));
  expect(screen.getByRole("heading", { name: "引用原文" })).toBeInTheDocument();
  await waitFor(() =>
    expect(api.runWorkflow).toHaveBeenCalledWith("medical-answer", {
      question: "如何复查？",
      provider: "qwen",
      top_k: 5,
    }),
  );
});

it("shows failed model runs as an alert instead of an assistant answer", async () => {
  const user = userEvent.setup();
  vi.mocked(api.runWorkflow).mockResolvedValue({
    run_id: "failed-1", status: "failed", output: {},
    trace: [{ node_id: "llm", status: "failed", duration_ms: 1, metadata: {},
      error: { code: "MODEL_RATE_LIMITED", message: "生成模型请求过于频繁，请稍后重试" } }],
  });
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "如何复查？");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("生成模型请求过于频繁");
  expect(screen.queryByText("知源助手")).not.toBeInTheDocument();
  expect(screen.queryByText("证据不足")).not.toBeInTheDocument();
});

it.each(["", "  \n  "])("reports an empty successful answer (%j) without saving an assistant message", async (answer) => {
  const user = userEvent.setup();
  vi.mocked(api.runWorkflow).mockResolvedValue({
    run_id: "empty-answer", status: "success",
    output: { answer, abstained: false, citations: [] }, trace: [],
  });
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "无答案问题");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("该工作流没有产生回答");
  expect(screen.queryByText("知源助手")).not.toBeInTheDocument();
  expect(JSON.parse(localStorage.getItem("zhiyuan-conversations-v1") || "[]")[0].messages)
    .toEqual([{ role: "user", content: "无答案问题" }]);
});

it("clears the failed-run alert when switching to an existing successful conversation", async () => {
  localStorage.setItem("zhiyuan-conversations-v1", JSON.stringify([{
    id: "successful", title: "已完成的问题", messages: [
      { role: "user", content: "已完成的问题" },
      { role: "assistant", content: "已核实的回答" },
    ],
  }]));
  const user = userEvent.setup();
  vi.mocked(api.runWorkflow).mockResolvedValue({
    run_id: "failed-2", status: "failed", output: {},
    trace: [{ node_id: "llm", status: "failed", duration_ms: 1, metadata: {},
      error: { code: "MODEL_RATE_LIMITED", message: "生成模型请求过于频繁，请稍后重试" } }],
  });
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "新的失败问题");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("生成模型请求过于频繁");
  await user.click(screen.getByRole("button", { name: /已完成的问题/ }));
  expect(screen.getByText("已核实的回答")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByText("新的失败问题", { selector: ".message-text" })).not.toBeInTheDocument();
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
    localStorage.setItem("zhiyuan-conversations-v1", JSON.stringify([{
      id: "successful", title: "已完成的问题", messages: [
        { role: "user", content: "已完成的问题" },
        { role: "assistant", content: "已核实的回答" },
      ],
    }]));
    let resolveRun!: (value: api.RunResult) => void;
    let rejectRun!: (reason: Error) => void;
    vi.mocked(api.runWorkflow).mockReturnValue(new Promise<api.RunResult>((resolve, reject) => {
      resolveRun = resolve;
      rejectRun = reject;
    }));
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
    await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "等待中的问题");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(api.runWorkflow).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: /已完成的问题/ }));
    expect(screen.getByText("已核实的回答")).toBeInTheDocument();
    await act(async () => {
      if (outcome === "failed") {
        resolveRun({ run_id: "late-failure", status: "failed", output: {},
          trace: [{ node_id: "llm", status: "failed", duration_ms: 1, metadata: {},
            error: { code: "MODEL_RATE_LIMITED", message: "迟到的失败" } }] });
      } else {
        rejectRun(new Error("迟到的请求错误"));
      }
    });
    expect(screen.getByText("已核实的回答")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("等待中的问题", { selector: ".message-text" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /等待中的问题/ }));
    expect(screen.getByRole("alert")).toHaveTextContent(outcome === "failed" ? "迟到的失败" : "迟到的请求错误");
  },
);

it("clears the conversation's error when retrying", async () => {
  const user = userEvent.setup();
  vi.mocked(api.runWorkflow).mockResolvedValueOnce({
    run_id: "failed-then-retry", status: "failed", output: {},
    trace: [{ node_id: "llm", status: "failed", duration_ms: 1, metadata: {},
      error: { code: "MODEL_RATE_LIMITED", message: "首次请求失败" } }],
  });
  let resolveRetry!: (value: api.RunResult) => void;
  vi.mocked(api.runWorkflow).mockImplementationOnce(() => new Promise<api.RunResult>((resolve) => {
    resolveRetry = resolve;
  }));
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "重试问题");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("首次请求失败");
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "再次尝试");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await act(async () => resolveRetry({ run_id: "retry-success", status: "success",
    output: { answer: "重试回答", abstained: false, citations: [] }, trace: [] }));
  expect(screen.getByText("重试回答")).toBeInTheDocument();
});

it("stores a late successful answer in its original conversation", async () => {
  localStorage.setItem("zhiyuan-conversations-v1", JSON.stringify([{
    id: "successful", title: "已完成的问题", messages: [
      { role: "user", content: "已完成的问题" },
      { role: "assistant", content: "已核实的回答" },
    ],
  }]));
  let resolveRun!: (value: api.RunResult) => void;
  vi.mocked(api.runWorkflow).mockReturnValue(new Promise<api.RunResult>((resolve) => {
    resolveRun = resolve;
  }));
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "等待中的问题");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await user.click(screen.getByRole("button", { name: /已完成的问题/ }));
  await act(async () => resolveRun({ run_id: "late-success", status: "success",
    output: { answer: "迟到的回答", abstained: false, citations: [] }, trace: [] }));
  expect(screen.getByText("已核实的回答")).toBeInTheDocument();
  expect(screen.queryByText("迟到的回答")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /等待中的问题/ }));
  expect(screen.getByText("迟到的回答")).toBeInTheDocument();
});

it("keeps old local conversations readable without citation metadata", async () => {
  localStorage.setItem("zhiyuan-conversations-v1", JSON.stringify([{
    id: "old", title: "旧问题", messages: [
      { role: "user", content: "旧问题" },
      { role: "assistant", content: "旧回答 [legacy]", citations: [{ filename: "旧资料" }], runId: "old-run" },
    ],
  }]));
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: /旧问题/ }));
  expect(screen.getByText("旧回答 [legacy]")).toBeInTheDocument();
  expect(screen.queryByText(/运行记录 #/)).not.toBeInTheDocument();
});

it("keeps workflow controls collapsed until requested", async () => {
  const user = userEvent.setup();
  vi.mocked(api.runWorkflow).mockResolvedValue({
    run_id: "run-2", status: "success",
    output: { answer: "资料不足", abstained: true, citations: [] }, trace: [],
  });
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  expect(screen.queryByRole("combobox", { name: "证据数" })).not.toBeInTheDocument();
  await user.click(screen.getByText("高级设置"));
  await user.selectOptions(screen.getByRole("combobox", { name: "证据数" }), "8");
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "高血压如何随访？");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() => expect(api.runWorkflow).toHaveBeenCalledWith("medical-answer", {
    question: "高血压如何随访？", provider: "qwen", top_k: 8,
  }));
});

it("renders verified claims as separate lines with compact source markers", async () => {
  const user = userEvent.setup();
  vi.mocked(api.runWorkflow).mockResolvedValue({
    run_id: "format-1", status: "success",
    output: { answer: "要点一 [block-1]\n要点二 [block-2]", abstained: false,
      citations: [
        { chunk_id: "block-1", filename: "指南.md", excerpt: "要点一" },
        { chunk_id: "block-2", filename: "问答.pdf", excerpt: "要点二" },
      ] },
    trace: [],
  });
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "有哪些要点？");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByText(/要点一/)).toBeInTheDocument();
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByRole("button", { name: "查看引用 1：指南.md" })).toBeInTheDocument();
});

it("renders each verified claim while the answer is still streaming", async () => {
  let release!: (value: api.RunResult) => void;
  vi.mocked(api.streamWorkflow).mockImplementation((_id, _inputs, onEvent) => {
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
  expect(screen.getByRole("button", { name: "来源 1：指南.md" })).toBeInTheDocument();
  // 同一条引用不会因为两条结论都引它而重复出现。
  expect(screen.getAllByRole("button", { name: "来源 1：指南.md" })).toHaveLength(1);

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
  vi.mocked(api.streamWorkflow).mockImplementation(async (_id, _inputs, onEvent) => {
    onEvent?.({
      event: "claim",
      data: {
        text: "应规律随访 [chunk-1]",
        citations: [{ chunk_id: "chunk-1", filename: "指南.md", excerpt: "规律随访" }],
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
        citations: [{ chunk_id: "chunk-1", filename: "指南.md", excerpt: "规律随访" }],
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
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "如何随访？");
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
  vi.mocked(api.streamWorkflow)
    .mockImplementationOnce(async (_id, _inputs, onEvent) => {
      onEvent?.(partial);
      return failed;
    })
    .mockImplementationOnce(async (_id, _inputs, onEvent) => {
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
  vi.mocked(api.streamWorkflow).mockImplementation((_id, _inputs, onEvent) => {
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
    await screen.findByText(/已检索到 6 条证据，正在生成回答（必要时联网补充）…/),
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

it("shows only complete answer workflows in the chat selector", async () => {
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
  const selector = await screen.findByRole("combobox", { name: "工作流" });
  expect(selector).toHaveValue("medical-answer");
  expect(screen.getByRole("option", { name: "医疗资料问答 · v1" })).toBeInTheDocument();
  expect(
    screen.getByRole("option", { name: "路由式医疗资料问答 · v1" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "医疗资料检索 · v1" })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "文档入库 · v1" })).not.toBeInTheDocument();
});

it("sends the next question through the workflow selected in chat", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getWorkflows).mockResolvedValue([workflow, expandedWorkflow]);
  vi.mocked(api.runWorkflow).mockResolvedValue({
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
  await user.selectOptions(
    await screen.findByRole("combobox", { name: "工作流" }),
    "medical-answer-expanded",
  );
  await user.type(
    screen.getByRole("textbox", { name: "输入医疗资料问题" }),
    "需要路由的问题",
  );
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() =>
    expect(api.runWorkflow).toHaveBeenCalledWith("medical-answer-expanded", {
      question: "需要路由的问题",
      provider: "qwen",
      top_k: 5,
    }),
  );
});

it("restores the workflow chosen on the editor page", async () => {
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
  expect(
    await screen.findByRole("combobox", { name: "工作流" }),
  ).toHaveValue("medical-answer-expanded");
});

it("publishes the workflow selected in chat to the editor page", async () => {
  const user = userEvent.setup();
  vi.mocked(api.getWorkflows).mockResolvedValue([workflow, expandedWorkflow]);
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  await user.selectOptions(
    await screen.findByRole("combobox", { name: "工作流" }),
    "medical-answer-expanded",
  );
  expect(localStorage.getItem("zhiyuan-current-workflow-v1")).toBe(
    "medical-answer-expanded",
  );
  expect(
    screen.getByRole("link", { name: /工作流编辑器/ }),
  ).toHaveAttribute("href", "/workflows/medical-answer-expanded");
});

it("does not send when no complete answer workflow is available", async () => {
  vi.mocked(api.getWorkflows).mockResolvedValue([searchWorkflow, ingestionWorkflow]);
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  expect(await screen.findByText("暂无可用的回答工作流")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
  expect(api.runWorkflow).not.toHaveBeenCalled();
});
