import { json, request } from "./client";
import { streamRun, type RunResult, type StreamEvent } from "./workflows";

export interface ChatPayload {
  request_id: string;
  conversation_id: string;
  workflow_id: string | null;
  inputs: Record<string, unknown>;
}
export interface ConfirmationPayload {
  request_id: string;
  conversation_id: string;
  token: string;
  accept: boolean;
}
export const streamChat = (
  payload: ChatPayload,
  onEvent?: (event: StreamEvent) => void,
  signal?: AbortSignal,
) => streamRun("/chat/runs/stream", { ...payload }, onEvent, signal);
export const confirmProfile = (payload: ConfirmationPayload) =>
  request<RunResult>("/chat/confirmations", json(payload));
