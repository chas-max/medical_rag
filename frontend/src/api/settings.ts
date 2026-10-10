import { json, request } from "./client";

/** Where the running model came from: a stored profile, the environment, or neither. */
export type ModelConfigSource = "settings" | "env" | "none";

/** One confirmed endpoint the operator can switch back to. The key never comes back. */
export interface ModelProfile {
  id: string;
  model_name: string;
  base_url: string;
  api_key_masked: string;
  verified_at: string | null;
  active: boolean;
}

export interface ModelConfig {
  model_name: string;
  base_url: string;
  api_key_masked: string;
  configured: boolean;
  source: ModelConfigSource;
  verified_at: string | null;
  active_id: string | null;
  profiles: ModelProfile[];
  /** The `.env` triple, shown so removing every profile does not look like data loss. */
  fallback: { model_name: string; base_url: string; configured: boolean };
}

export interface ModelConfigInput {
  model_name: string;
  base_url: string;
  api_key: string;
}

export const getModelConfig = () => request<ModelConfig>("/settings/model");

/** Verifies the triple against the real endpoint, then stores it as the active profile. */
export const saveModelConfig = (input: ModelConfigInput) =>
  request<ModelConfig>("/settings/model", json(input));

/** Switches to a stored profile without probing it again. */
export const selectModelProfile = (profileId: string) =>
  request<ModelConfig>("/settings/model/select", json({ profile_id: profileId }));

export const deleteModelProfile = (profileId: string) =>
  request<ModelConfig>(`/settings/model/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
  });
