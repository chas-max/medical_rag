/** Server-Sent Events client for the patient answer stream.
 *
 * `EventSource` cannot be used here: it neither sends a request body nor lets the
 * caller attach the `X-CSRF-Token` header this portal requires for a write. The
 * reader below is therefore a minimal, bounded SSE parser over `fetch`.
 *
 * Frames before `done` are provisional display material. The resolved value is the
 * `done` payload and the only thing a caller may treat as the answer.
 */

export class StreamError extends Error {
  constructor(
    public readonly code: string,
    public readonly status: number,
  ) {
    super("问答服务暂不可用，请稍后重试。");
  }
}

export type StreamHandlers = {
  onStatus?: (stage: string) => void;
  onClaim?: (text: string) => void;
};

const MAX_FRAME = 16 * 1024;

function parse(block: string): { event: string; data: unknown } | null {
  let name = "";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") name = value;
    else if (field === "data") data.push(value);
  }
  if (!name || data.length === 0) return null;
  try {
    return { event: name, data: JSON.parse(data.join("\n")) };
  } catch {
    throw new StreamError("INVALID_STREAM", 503);
  }
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export async function streamChat(
  path: string,
  body: unknown,
  csrf: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-CSRF-Token": csrf,
      },
      body: JSON.stringify(body),
      signal,
    });
  } catch {
    throw new StreamError("NETWORK", 503);
  }
  // A failure before the stream starts is still the JSON envelope the rest of the
  // portal uses, so its status code is meaningful and kept.
  if (!response.ok || !response.body)
    throw new StreamError("HTTP", response.status);
  if (
    response.headers.get("content-type")?.split(";")[0].trim() !==
    "text/event-stream"
  )
    throw new StreamError("INVALID_STREAM", 503);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let answer: unknown = null;
  let settled = false;

  const apply = (frame: { event: string; data: unknown }) => {
    if (frame.event === "error") {
      const code =
        object(frame.data) && typeof frame.data.code === "string"
          ? frame.data.code
          : "CHAT_UNAVAILABLE";
      const status =
        object(frame.data) && typeof frame.data.status === "number"
          ? frame.data.status
          : 503;
      throw new StreamError(code, status);
    }
    if (frame.event === "done") {
      answer = frame.data;
      settled = true;
      return;
    }
    if (frame.event === "status") {
      if (object(frame.data) && typeof frame.data.stage === "string")
        handlers.onStatus?.(frame.data.stage);
      return;
    }
    if (frame.event === "claim") {
      if (object(frame.data) && typeof frame.data.text === "string")
        handlers.onClaim?.(frame.data.text);
    }
    // Unknown event names are ignored so a newer server cannot break an older UI.
  };

  try {
    while (!settled) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Normalise first: a CRLF-only buffer would otherwise look like a frame
      // that never terminates.
      buffer = buffer.replace(/\r\n/g, "\n");
      if (buffer.length > MAX_FRAME && !buffer.includes("\n\n"))
        throw new StreamError("INVALID_STREAM", 503);
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0 && !settled) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const frame = parse(block);
        if (frame) apply(frame);
        boundary = buffer.indexOf("\n\n");
      }
    }
    if (!settled && buffer.trim()) {
      const frame = parse(buffer);
      if (frame) apply(frame);
    }
  } finally {
    if (!settled) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  // A stream that ends without the terminal frame is a failed turn, never a
  // short answer: the caller must not render or persist a partial result.
  if (!settled) throw new StreamError("INCOMPLETE", 503);
  return answer;
}
