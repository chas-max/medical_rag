import { afterEach, expect, it, vi } from "vitest";
import { confirmProfile, streamChat } from "../api/chat";
import { ApiRequestError } from "../api/client";
import type { RunResult, StreamEvent } from "../api/workflows";

afterEach(() => vi.unstubAllGlobals());
const payload = {
  request_id: "q1",
  conversation_id: "c1",
  workflow_id: null,
  inputs: { question: "我是一名护士" },
  memory_consent: false,
};
const result: RunResult = {
  run_id: "r1",
  status: "success",
  kind: "memory",
  output: { answer: "已记录" },
  trace: [],
  profile: {
    status: "processed",
    actions: [
      { key: "occupation", status: "added", message: "职业画像已新增" },
    ],
  },
  episode: { status: "skipped" },
};
function response(value: RunResult, progress = false) {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            (progress
              ? 'event: chat.progress\ndata: {"stage":"intent"}\n\n'
              : "") + `event: done\ndata: ${JSON.stringify(value)}\n\n`,
          ),
        );
        controller.close();
      },
    }),
  );
}
it("sends a stable chat request without a selected workflow, including retries", async () => {
  const fetchMock = vi.fn().mockImplementation(async () => response(result));
  vi.stubGlobal("fetch", fetchMock);
  expect(await streamChat(payload)).toEqual(result);
  await streamChat(payload);
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/api/chat/runs/stream",
    "/api/chat/runs/stream",
  ]);
  for (const [, init] of fetchMock.mock.calls)
    expect(JSON.parse(init.body)).toEqual(payload);
});
it("preserves progress and independent profile outcomes in a failed mixed result", async () => {
  const mixed: RunResult = {
    ...result,
    status: "failed",
    kind: "mixed",
    error: { code: "RETRIEVAL", message: "检索不可用" },
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(mixed, true)));
  const seen: StreamEvent[] = [];
  expect(await streamChat(payload, (event) => seen.push(event))).toEqual(mixed);
  expect(seen).toEqual([
    { event: "chat.progress", data: { stage: "intent" } },
    { event: "done", data: mixed },
  ]);
});
it("posts a bound explicit confirmation and returns the run envelope data", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValue(
      new Response(
        JSON.stringify({ success: true, data: result, error: null }),
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  const confirmation = {
    request_id: "confirm-1",
    conversation_id: "c1",
    token: "bound-token",
    accept: false,
    memory_consent: false,
  };
  expect(await confirmProfile(confirmation)).toEqual(result);
  expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/confirmations");
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(confirmation);
});

it.each([
  "MEMORY_CONFIRMATION_EXPIRED",
  "MEMORY_CONFIRMATION_CONFLICT",
  "MEMORY_CONFIRMATION_INVALID",
])("preserves a terminal HTTP409 %s confirmation error", async (code) => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          success: false,
          data: null,
          error: { code, message: "确认已失效，请重新提出变更" },
        }),
        { status: 409 },
      ),
    ),
  );
  const failure = await confirmProfile({
    request_id: "confirm-1",
    conversation_id: "c1",
    token: "bound-token",
    accept: true,
  }).catch((error) => error);
  expect(failure).toBeInstanceOf(ApiRequestError);
  expect(failure).toMatchObject({
    code,
    status: 409,
    message: "确认已失效，请重新提出变更",
  });
});
