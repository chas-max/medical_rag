import { afterEach, expect, it, vi } from "vitest";
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
  const fetchMock = vi.fn().mockResolvedValue(
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
    vi.fn().mockResolvedValue(
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
    vi.fn().mockResolvedValue(
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
