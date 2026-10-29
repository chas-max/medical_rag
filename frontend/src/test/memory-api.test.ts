import { afterEach, expect, it, vi } from "vitest";
import * as memory from "../api/memory";

afterEach(() => vi.unstubAllGlobals());

it("uses encoded memory endpoints and explicit request bodies", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValue({
      ok: true,
      json: async () => ({ success: true, data: {}, error: null }),
    });
  vi.stubGlobal("fetch", fetchMock);
  await memory.getMemory();
  await memory.setMemoryEnabled(false);
  await memory.saveMemoryProfile("display_name", {
    value: "小明",
    category: "background",
    sensitive: false,
  });
  await memory.deleteMemoryProfile("name / test");
  await memory.getMemoryEpisode("episode / 1");
  await memory.deleteMemoryEpisode("episode / 1");
  await memory.clearMemory();
  await memory.getMemoryJob("run / 1");
  expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
    "/api/memory",
    "/api/memory/settings",
    "/api/memory/profiles/display_name",
    "/api/memory/profiles/name%20%2F%20test",
    "/api/memory/episodes/episode%20%2F%201",
    "/api/memory/episodes/episode%20%2F%201",
    "/api/memory",
    "/api/memory/jobs/run%20%2F%201",
  ]);
  expect(fetchMock.mock.calls[1][1].method).toBe("PUT");
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
    enabled: false,
  });
  expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({
    value: "小明",
    category: "background",
    sensitive: false,
  });
  expect(fetchMock.mock.calls[3][1].method).toBe("DELETE");
  expect(fetchMock.mock.calls[5][1].method).toBe("DELETE");
  expect(fetchMock.mock.calls[6][1].method).toBe("DELETE");
});
