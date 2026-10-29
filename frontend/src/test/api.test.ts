import { afterEach, expect, it, vi } from "vitest";
import {
  getConversations,
  getConversation,
  importConversations,
} from "../api/conversations";
import {
  getHealth,
  getNodes,
  getWorkflow,
  getWorkflows,
  runWorkflow,
  saveWorkflow,
  streamWorkflow,
  uploadDocument,
  validateWorkflow,
  type StreamEvent,
  type Workflow,
} from "../api/workflows";

afterEach(() => vi.unstubAllGlobals());

it("uses durable history endpoints and links a streamed run without changing graph inputs", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ success: true, data: [], error: null }),
  });
  vi.stubGlobal("fetch", fetchMock);
  await getConversations();
  await getConversation("chat / 1");
  await importConversations([]);
  expect(fetchMock.mock.calls[0][0]).toBe("/api/conversations");
  expect(fetchMock.mock.calls[1][0]).toBe("/api/conversations/chat%20%2F%201");
  expect(fetchMock.mock.calls[2][0]).toBe("/api/conversations/import");
  expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({
    conversations: [],
  });
  fetchMock.mockResolvedValue(
    streamingResponse([
      'event: done\ndata: {"run_id":"history-run","status":"success","output":{},"trace":[]}\n\n',
    ]),
  );
  await streamWorkflow(
    "medical-answer",
    { question: "问题" },
    undefined,
    undefined,
    "chat-1",
  );
  expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual({
    inputs: { question: "问题" },
    conversation_id: "chat-1",
    memory_consent: false,
  });
});

it("sends per-message memory consent outside workflow inputs", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValue(
      streamingResponse([
        'event: done\ndata: {"run_id":"memory-run","status":"success","output":{},"trace":[],"memory":{"status":"pending"}}\n\n',
      ]),
    );
  vi.stubGlobal("fetch", fetchMock);
  const result = await streamWorkflow(
    "medical-answer",
    { question: "我的自述" },
    undefined,
    undefined,
    "chat-1",
    true,
  );
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
    inputs: { question: "我的自述" },
    conversation_id: "chat-1",
    memory_consent: true,
  });
  expect(result.memory?.status).toBe("pending");
});

it("loads an exact encoded workflow version and forwards container progress", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ success: true, data: {}, error: null }),
  });
  vi.stubGlobal("fetch", fetchMock);
  await getWorkflow("child demo", 3);
  expect(fetchMock.mock.calls[0][0]).toBe(
    "/api/workflows/child%20demo?version=3",
  );
  const progress = {
    node_id: "agent",
    stage: "assessing",
    iteration: 0,
    evidence: 2,
  };
  fetchMock.mockResolvedValue(
    streamingResponse([
      `event: node.progress\ndata: ${JSON.stringify(progress)}\n\n`,
      'event: done\ndata: {"run_id":"r","status":"success","output":{},"trace":[]}\n\n',
    ]),
  );
  const seen: StreamEvent[] = [];
  await streamWorkflow("parent", {}, (event) => seen.push(event));
  expect(seen[0]).toEqual({ event: "node.progress", data: progress });
});

/** Minimal stand-in for a fetch Response whose body arrives in fixed chunks. */
function streamingResponse(chunks: string[]) {
  let index = 0;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () =>
          index < chunks.length
            ? { value: new TextEncoder().encode(chunks[index++]), done: false }
            : { value: undefined, done: true },
        cancel: async () => undefined,
        releaseLock: () => undefined,
      }),
    },
  };
}

it("unwraps the success/data/error envelope", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        success: true,
        data: [{ type: "query.input" }],
        error: null,
      }),
    }),
  );
  await expect(getNodes()).resolves.toEqual([{ type: "query.input" }]);
});

it("preserves safe server error messages", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({
        success: false,
        data: null,
        error: { code: "INVALID_WORKFLOW", message: "工作流无效" },
      }),
    }),
  );
  await expect(getNodes()).rejects.toThrow("工作流无效");
});

it("uses the workflow and upload endpoints with the expected HTTP payloads", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ success: true, data: [], error: null }),
  });
  vi.stubGlobal("fetch", fetchMock);
  const workflow: Workflow = {
    id: "answer demo",
    name: "Demo",
    version: 2,
    status: "draft",
    nodes: [],
    edges: [],
  };
  await getWorkflows();
  await getWorkflow(workflow.id);
  await validateWorkflow(workflow);
  await saveWorkflow(workflow);
  await runWorkflow(workflow.id, { question: "测试" });
  await getHealth();
  await uploadDocument(new File(["文本"], "guide.txt"));
  expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
    "/api/workflows",
    "/api/workflows/answer%20demo",
    "/api/workflows/answer%20demo/validate",
    "/api/workflows/answer%20demo",
    "/api/workflows/answer%20demo/runs",
    "/api/health",
    "/api/documents",
  ]);
  expect(fetchMock.mock.calls[3][1].method).toBe("PUT");
  expect(JSON.parse(fetchMock.mock.calls[4][1].body)).toEqual({
    inputs: { question: "测试" },
  });
  expect(fetchMock.mock.calls[6][1].body).toBeInstanceOf(FormData);
});

it("handles network and non-JSON responses safely", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockRejectedValue(new Error("private details")),
  );
  await expect(getNodes()).rejects.toThrow("无法连接服务");
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: false,
      json: async () => {
        throw new Error("private details");
      },
    }),
  );
  await expect(getNodes()).rejects.toThrow("无法解析");
});

it("streams verified claims and resolves with the authoritative run", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValue(
      streamingResponse([
        'event: status\ndata: {"stage":"generating","evidence":3,"web_search":true}\n\n',
        'event: claim\ndata: {"text":"第一条 [c1]","citations":[{"chunk_id":"c1"}]}\n\n',
        'event: claim\ndata: {"text":"第二',
        '条 [c2]","citations":[{"chunk_id":"c2"}]}\n\n',
        'event: analysis\ndata: {"summary":"两条都指向长期管理。","extensions":[],"based_on":["c1"]}\n\n',
        'event: done\ndata: {"run_id":"run-1","status":"success","output":{"abstained":false,"answer":"第一条 [c1]\\n第二条 [c2]"},"trace":[]}\n\n',
      ]),
    );
  vi.stubGlobal("fetch", fetchMock);
  const seen: StreamEvent[] = [];
  const result = await streamWorkflow(
    "medical-answer",
    { question: "高血压" },
    (event) => seen.push(event),
  );

  expect(fetchMock.mock.calls[0][0]).toBe(
    "/api/workflows/medical-answer/runs/stream",
  );
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
    inputs: { question: "高血压" },
    memory_consent: false,
  });
  // 解释段固定排在所有结论之后、done 之前，前端据此分两段渲染。
  expect(seen.map((event) => event.event)).toEqual([
    "status",
    "claim",
    "claim",
    "analysis",
    "done",
  ]);
  expect(seen[0].data).toEqual({
    stage: "generating",
    evidence: 3,
    web_search: true,
  });
  expect(seen[1].data).toEqual({
    text: "第一条 [c1]",
    citations: [{ chunk_id: "c1" }],
  });
  expect(seen[2].data).toEqual({
    text: "第二条 [c2]",
    citations: [{ chunk_id: "c2" }],
  });
  expect(seen[3].data).toEqual({
    summary: "两条都指向长期管理。",
    extensions: [],
    based_on: ["c1"],
  });
  expect(result.run_id).toBe("run-1");
});

it("reports a streamed failure and an interrupted stream as errors", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        streamingResponse([
          'event: error\ndata: {"code":"INVALID_QUERY","message":"问题不能为空","status":422}\n\n',
        ]),
      ),
  );
  await expect(streamWorkflow("medical-answer", {})).rejects.toThrow(
    "问题不能为空",
  );

  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        streamingResponse([
          'event: claim\ndata: {"text":"半截 [c1]","citations":[]}\n\n',
        ]),
      ),
  );
  await expect(streamWorkflow("medical-answer", {})).rejects.toThrow(
    "流式响应意外中断",
  );
});

it("keeps the pre-stream error envelope for the streaming route", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({
        success: false,
        data: null,
        error: { code: "NOT_READY", message: "服务尚未初始化" },
      }),
    }),
  );
  await expect(streamWorkflow("medical-answer", {})).rejects.toThrow(
    "服务尚未初始化",
  );
});

it("preserves the legacy conversation and consent body when extracting the shared stream runner", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValue(
      streamingResponse([
        'event: done\ndata: {"run_id":"legacy","status":"success","output":{"answer":"旧入口"},"trace":[]}\n\n',
      ]),
    );
  vi.stubGlobal("fetch", fetchMock);
  const controller = new AbortController();
  await streamWorkflow(
    "answer flow",
    { question: "原问题", top_k: 8 },
    undefined,
    controller.signal,
    "old-conversation",
    true,
  );
  expect(fetchMock.mock.calls[0][0]).toBe(
    "/api/workflows/answer%20flow/runs/stream",
  );
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
    inputs: { question: "原问题", top_k: 8 },
    conversation_id: "old-conversation",
    memory_consent: true,
  });
  expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
});
