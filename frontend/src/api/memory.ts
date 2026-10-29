import { json, request } from "./client";

export type MemoryCategory =
  "background" | "preferences" | "interests" | "health";
export interface MemoryProfile {
  key: string;
  value: string;
  category: MemoryCategory;
  sensitive: boolean;
  source: string;
  support: string;
  source_run_id: string | null;
}
export interface MemoryEpisode {
  id: string;
  title: string;
  created_at: string;
  source_run_id: string;
  sensitive: boolean;
}
export type MemoryJobStatus =
  "pending" | "processing" | "saved" | "failed" | "cancelled" | "skipped";
export interface MemoryJob {
  run_id: string;
  status: MemoryJobStatus;
  error: string | null;
}
export interface MemoryState {
  enabled: boolean;
  revision: number;
  profiles: MemoryProfile[];
  episodes: MemoryEpisode[];
  jobs: MemoryJob[];
}
export const getMemory = () => request<MemoryState>("/memory");
export const setMemoryEnabled = (enabled: boolean) =>
  request<MemoryState>("/memory/settings", {
    ...json({ enabled }),
    method: "PUT",
  });
export const saveMemoryProfile = (
  key: string,
  profile: { value: string; category: MemoryCategory; sensitive: boolean },
) =>
  request<MemoryState>(`/memory/profiles/${encodeURIComponent(key)}`, {
    ...json(profile),
    method: "PUT",
  });
export const deleteMemoryProfile = (key: string) =>
  request<MemoryState>(`/memory/profiles/${encodeURIComponent(key)}`, {
    method: "DELETE",
  });
export const getMemoryEpisode = (id: string) =>
  request<{ id: string; content: string }>(
    `/memory/episodes/${encodeURIComponent(id)}`,
  );
export const deleteMemoryEpisode = (id: string) =>
  request<MemoryState>(`/memory/episodes/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
export const clearMemory = () =>
  request<MemoryState>("/memory", { method: "DELETE" });
export const getMemoryJob = (runId: string, signal?: AbortSignal) =>
  request<MemoryJob>(`/memory/jobs/${encodeURIComponent(runId)}`, { signal });
