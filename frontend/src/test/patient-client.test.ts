import { afterEach, describe, expect, it, vi } from "vitest";
import { PatientError, request } from "../patient/client";

afterEach(() => vi.unstubAllGlobals());
describe("patient same-origin transport", () => {
  it("uses cookie credentials/no-store and exact CSRF JSON without ownership", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ success: true, data: null, error: null }),
          { headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetch);
    expect(await request("/auth/logout", "POST", undefined, "csrf")).toBeNull();
    expect(fetch).toHaveBeenCalledWith(
      "/auth/logout",
      expect.objectContaining({
        credentials: "same-origin",
        cache: "no-store",
        method: "POST",
        headers: { "X-CSRF-Token": "csrf" },
      }),
    );
  });
  it.each([
    "secret",
    { success: true, data: {}, error: "secret" },
    { success: "yes", data: null, error: null },
    { success: false, data: "secret", error: { message: "secret" } },
  ])("rejects malformed envelopes without echo", async (data) => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify(data), {
            headers: { "Content-Type": "application/json" },
          }),
        ),
    );
    await expect(request("/me")).rejects.toThrow("服务暂不可用，请重试。");
  });
  it("sanitizes HTTP errors, non JSON, parse and network failures", async () => {
    for (const response of [
      new Response("secret", { status: 403 }),
      new Response("secret"),
      new Response("secret", {
        headers: { "Content-Type": "application/json" },
      }),
    ]) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
      await expect(request("/me")).rejects.toBeInstanceOf(PatientError);
    }
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(Error("secret")));
    await expect(request("/me")).rejects.toThrow("服务暂不可用，请重试。");
  });
});
