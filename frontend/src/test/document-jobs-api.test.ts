import { afterEach, expect, it, vi } from "vitest";
import { getDocumentJob, getDocumentJobs } from "../api/documents";

afterEach(() => vi.unstubAllGlobals());

it("queries durable upload state using encoded IDs and bounded list options", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, data: [], error: null }),
    });
  vi.stubGlobal("fetch", fetch);
  await getDocumentJob("a/b");
  await getDocumentJobs();
  await getDocumentJobs(false, 50);
  expect(fetch.mock.calls.map(([url]) => url)).toEqual([
    "/api/document-jobs/a%2Fb",
    "/api/document-jobs?active=true&limit=50",
    "/api/document-jobs?active=false&limit=50",
  ]);
});
