import { afterEach, expect, it, vi } from "vitest";
import { StreamError, streamChat } from "../patient/stream";

const encoder = new TextEncoder();

function respond(chunks: string[], contentType = "text/event-stream") {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { headers: { "Content-Type": contentType } },
  );
}

function mock(chunks: string[], contentType?: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => respond(chunks, contentType)),
  );
}

afterEach(() => vi.unstubAllGlobals());

it("reassembles a frame split across network chunks", async () => {
  mock([
    'event: claim\ndata: {"te',
    'xt":"结论一。"}\n\nevent: done\ndata: {"answer":"完整"}\n\n',
  ]);
  const claims: string[] = [];

  const answer = await streamChat("/chat/stream", {}, "csrf", {
    onClaim: (text) => claims.push(text),
  });

  expect(claims).toEqual(["结论一。"]);
  expect(answer).toEqual({ answer: "完整" });
});

it("accepts CRLF framing and ignores comments", async () => {
  mock([
    ": keep-alive\r\nevent: status\r\ndata: {\"stage\":\"generating\"}\r\n\r\nevent: done\r\ndata: {\"answer\":\"完整\"}\r\n\r\n",
  ]);
  const stages: string[] = [];

  const answer = await streamChat("/chat/stream", {}, "csrf", {
    onStatus: (stage) => stages.push(stage),
  });

  expect(stages).toEqual(["generating"]);
  expect(answer).toEqual({ answer: "完整" });
});

it("ignores unknown events so a newer server cannot break the page", async () => {
  mock([
    'event: future\ndata: {"x":1}\n\nevent: done\ndata: {"answer":"完整"}\n\n',
  ]);

  await expect(streamChat("/chat/stream", {}, "csrf", {})).resolves.toEqual({
    answer: "完整",
  });
});

it("treats a stream that ends without done as a failure", async () => {
  mock(['event: claim\ndata: {"text":"结论一。"}\n\n']);
  const claims: string[] = [];

  await expect(
    streamChat("/chat/stream", {}, "csrf", { onClaim: (t) => claims.push(t) }),
  ).rejects.toMatchObject({ code: "INCOMPLETE", status: 503 });
  // The provisional text was received, but the caller must never treat it as
  // the answer: the promise rejected instead of resolving.
  expect(claims).toEqual(["结论一。"]);
});

it("keeps the upstream status code of a failure frame", async () => {
  mock([
    'event: error\ndata: {"code":"CONVERSATION_CONTEXT_CHANGED","status":409}\n\n',
  ]);

  const failure = await streamChat("/chat/stream", {}, "csrf", {}).catch(
    (error: unknown) => error,
  );

  expect(failure).toBeInstanceOf(StreamError);
  expect(failure).toMatchObject({
    code: "CONVERSATION_CONTEXT_CHANGED",
    status: 409,
  });
});

it("rejects a non-event-stream response instead of parsing HTML", async () => {
  mock(["<html>login</html>"], "text/html");

  await expect(streamChat("/chat/stream", {}, "csrf", {})).rejects.toMatchObject({
    code: "INVALID_STREAM",
  });
});

it("sends the CSRF header and an event-stream Accept header", async () => {
  mock(['event: done\ndata: {"answer":"完整"}\n\n']);

  await streamChat("/chat/stream", { question: "问题" }, "csrf-memory", {});

  expect(fetch).toHaveBeenCalledWith(
    "/chat/stream",
    expect.objectContaining({
      method: "POST",
      credentials: "same-origin",
      body: JSON.stringify({ question: "问题" }),
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-CSRF-Token": "csrf-memory",
      },
    }),
  );
});
