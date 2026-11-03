export class PatientError extends Error {
  constructor(public readonly status = 503) {
    super("服务暂不可用，请重试。");
  }
}
export function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
export async function request(
  path: string,
  method = "GET",
  body?: unknown,
  csrf?: string,
): Promise<unknown> {
  try {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (csrf !== undefined) headers["X-CSRF-Token"] = csrf;
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers,
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) throw new PatientError(response.status);
    if (
      response.headers.get("content-type")?.split(";")[0].trim() !==
      "application/json"
    )
      throw new PatientError();
    const result: unknown = await response.json();
    if (
      !object(result) ||
      result.success !== true ||
      !("data" in result) ||
      result.error !== null
    )
      throw new PatientError();
    return result.data;
  } catch (error) {
    throw error instanceof PatientError ? error : new PatientError();
  }
}
