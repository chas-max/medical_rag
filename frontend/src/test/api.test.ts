import { afterEach, expect, it, vi } from "vitest";
import {
  getHealth,
  getNodes,
  getWorkflow,
  getWorkflows,
  runWorkflow,
  saveWorkflow,
  uploadDocument,
  validateWorkflow,
  type Workflow,
} from "../api/workflows";

afterEach(() => vi.unstubAllGlobals());

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
