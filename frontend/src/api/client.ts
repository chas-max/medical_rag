/**
 * Shared transport for every settings and workflow endpoint: the dev server proxies
 * `/api` to the backend, while a built bundle is served by FastAPI itself.
 */
export const base = import.meta.env.DEV ? "/api" : "";

/** Preserve public application error metadata without leaking transport internals. */
export class ApiRequestError extends Error {
  constructor(
    message: string,
    public readonly code: string | undefined,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, init);
  } catch {
    throw new Error("无法连接服务，请确认后端已经启动");
  }
  let envelope: {
    success: boolean;
    data: T;
    error: { message: string; code?: string } | null;
  };
  try {
    envelope = await response.json();
  } catch {
    throw new Error("服务返回了无法解析的响应");
  }
  if (!response.ok || !envelope.success)
    throw new ApiRequestError(
      envelope.error?.message || "请求失败",
      envelope.error?.code,
      response.status,
    );
  return envelope.data;
}

export const json = (value: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
});
