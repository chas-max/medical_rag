import { base, json, request } from "./client";

/**
 * One input port. `optional` ports may stay unconnected, `collect` ports accept
 * several edges and receive them as a list, and `requires_tag` names a safety
 * step every path into this port must pass through.
 */
export interface PortInfo {
  dtype: string;
  optional: boolean;
  collect: boolean;
  requires_tag: string | null;
}

export interface NodeMetadata {
  workflow_kinds?: WorkflowKind[];
  type: string;
  title: string;
  category: string;
  inputs: Record<string, PortInfo>;
  outputs: Record<string, string>;
  /** 该节点为其输出背书的安全环节。 */
  tags?: string[];
  /** 这些端口的值必须来自同一个上游节点。 */
  shared_producers?: [string, string][];
  /** 输入/输出类节点常驻工作流，画布上不可随手删除。 */
  fixed?: boolean;
  /** 同类别缺失时由编辑器补回的规范节点。 */
  canonical?: boolean;
  config_schema: Record<
    string,
    {
      type: string;
      default?: number | string | boolean;
      minimum?: number;
      maximum?: number;
      enum?: (string | number)[];
    }
  >;
}

/** 节点实例级的路由条件：某个入参等于指定值时才执行。 */
export interface NodeActivation {
  port: string;
  equals: string | number | boolean | null;
}

export interface WorkflowNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  config: Record<string, string | number | boolean | null>;
  activation?: NodeActivation | null;
}

export interface WorkflowEdge {
  id: string;
  source: string;
  source_port: string;
  target: string;
  target_port: string;
}

export type WorkflowKind = "workflow" | "agent_iteration";

export interface Workflow {
  kind?: WorkflowKind;
  id: string;
  name: string;
  version: number;
  status: "draft" | "validated" | "published";
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
}

export interface Citation {
  id?: string;
  chunk_id?: string;
  source?: string;
  filename?: string;
  page?: number | null;
  excerpt?: string;
  score?: number;
  [key: string]: unknown;
}

export type ProfileActionStatus =
  | "added"
  | "updated"
  | "deleted"
  | "unchanged"
  | "blocked"
  | "needs_confirmation"
  | "conflict"
  | "unsupported"
  | "failed";
export interface ProfileOutcome {
  status: "processed" | "skipped" | "disabled" | "failed" | "conflict";
  actions: { key: string; status: ProfileActionStatus; message: string }[];
  confirmation_token?: string | null;
  confirmation_expires_at?: number | null;
  confirmation_requires_consent?: boolean;
}
export interface EpisodeOutcome {
  status:
    "pending" | "processing" | "saved" | "skipped" | "failed" | "cancelled";
}
export interface RunResult {
  kind?: "memory" | "answer" | "mixed";
  profile?: ProfileOutcome;
  episode?: EpisodeOutcome;
  error?: { code: string; message: string } | null;
  memory?: { status: string };
  evidence?: Citation[];
  run_id: string;
  status: "success" | "failed";
  output: {
    answer?: string;
    citations?: Citation[];
    abstained?: boolean;
    /** 拒答原因码：区分“资料不足”与解析失败/截断/引用校验失败。 */
    abstain_reason?: string;
    /** 澄清轮次不是医学结论：界面按补充问题展示，不显示“证据不足”。 */
    answer_type?: "answer" | "clarification";
    evidence?: Citation[];
    [key: string]: unknown;
  };
  trace: {
    node_id: string;
    status: string;
    duration_ms: number;
    metadata: Record<string, unknown>;
    error: { code: string; message: string } | null;
  }[];
}

export interface ValidationResult {
  valid: boolean;
  issues: {
    code: string;
    message: string;
    node_id?: string;
    edge_id?: string;
  }[];
}

/** One claim whose excerpt already passed verbatim verification on the server. */
export interface ClaimPayload {
  text: string;
  citations: Citation[];
}

/**
 * The interpretive section: it may expand on the verified claims, but it carries
 * no facts of its own and always points back at blocks the claims already cited.
 */
export interface AnalysisPayload {
  summary: string;
  extensions: string[];
  based_on: string[];
}

/** Progress notice so a long generation never looks like a stalled request. */
export interface StatusPayload {
  stage: string;
  evidence: number;
  web_search: boolean;
}

export interface NodeProgressPayload {
  node_id: string;
  stage: "assessing" | "planning" | "retrieving" | "stopped";
  iteration: number;
  evidence: number;
  stop_reason?: string;
}

export type StreamEvent =
  | { event: "chat.progress"; data: { stage: "intent" | "profile" | "answer" } }
  | { event: "node.progress"; data: NodeProgressPayload }
  | { event: "status"; data: StatusPayload }
  | { event: "claim"; data: ClaimPayload }
  | { event: "analysis"; data: AnalysisPayload }
  | { event: "done"; data: RunResult }
  | {
      event: "error";
      data: { code: string; message: string; status?: number };
    };

function parseFrame(block: string): { event: string; data: unknown } | null {
  let name = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const raw = separator < 0 ? "" : line.slice(separator + 1);
    const value = raw.startsWith(" ") ? raw.slice(1) : raw;
    if (field === "event") name = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  try {
    return { event: name, data: JSON.parse(data.join("\n")) };
  } catch {
    throw new Error("服务返回了无法解析的流式响应");
  }
}

/**
 * Run a published workflow over SSE, handing every verified claim to `onEvent`
 * as soon as the server publishes it. The interpretive section arrives last, as
 * one `analysis` event. Resolves with the authoritative run result, which the
 * caller should treat as the final answer.
 */
export async function streamRun(
  path: string,
  payload: Record<string, unknown>,
  onEvent?: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<RunResult> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(payload),
      signal,
    });
  } catch {
    throw new Error("无法连接服务，请确认后端已经启动");
  }
  if (!response.ok || !response.body) {
    // 流开始前的失败仍然是 {success,data,error} 信封，例如 NOT_READY。
    let message = "请求失败";
    try {
      const envelope = (await response.json()) as {
        error?: { message?: string } | null;
      };
      message = envelope?.error?.message || message;
    } catch {
      // 保留默认提示。
    }
    throw new Error(message);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const state: { result: RunResult | null } = { result: null };
  let buffer = "";
  const consume = (block: string) => {
    const frame = parseFrame(block);
    if (!frame) return;
    if (frame.event === "chat.progress") {
      onEvent?.({
        event: "chat.progress",
        data: frame.data as { stage: "intent" | "profile" | "answer" },
      });
    } else if (frame.event === "node.progress") {
      onEvent?.({
        event: "node.progress",
        data: frame.data as NodeProgressPayload,
      });
    } else if (frame.event === "status") {
      onEvent?.({ event: "status", data: frame.data as StatusPayload });
    } else if (frame.event === "claim") {
      onEvent?.({ event: "claim", data: frame.data as ClaimPayload });
    } else if (frame.event === "analysis") {
      onEvent?.({ event: "analysis", data: frame.data as AnalysisPayload });
    } else if (frame.event === "done") {
      state.result = frame.data as RunResult;
      onEvent?.({ event: "done", data: state.result });
    } else if (frame.event === "error") {
      const failure = frame.data as { message?: string } | null;
      throw new Error(failure?.message || "请求失败");
    }
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        consume(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
    if (buffer.trim()) consume(buffer);
  } finally {
    if (!state.result) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  if (!state.result) throw new Error("流式响应意外中断，请重试");
  return state.result;
}

/** Compatibility wrapper: keep legacy workflow callers and request bodies intact. */
export function streamWorkflow(
  id: string,
  inputs: Record<string, unknown>,
  onEvent?: (event: StreamEvent) => void,
  signal?: AbortSignal,
  conversationId?: string,
  memoryConsent = false,
): Promise<RunResult> {
  return streamRun(
    `/workflows/${encodeURIComponent(id)}/runs/stream`,
    {
      inputs,
      conversation_id: conversationId,
      memory_consent: memoryConsent,
    },
    onEvent,
    signal,
  );
}

export interface ChunkDetail {
  chunk_id: string;
  document_id: string;
  source_id: string;
  filename: string;
  section: string;
  page: number | null;
  ordinal: number;
  text: string;
  parent_text: string;
  parent_ordinal: number;
  token_count: number;
}

export const getNodes = () => request<NodeMetadata[]>("/nodes");
export const getWorkflows = () => request<Workflow[]>("/workflows");
export const getChunk = (chunkId: string) =>
  request<ChunkDetail>(`/chunks/${encodeURIComponent(chunkId)}`);
export const getWorkflow = (id: string, version?: number) =>
  request<Workflow>(
    `/workflows/${encodeURIComponent(id)}${version === undefined ? "" : `?version=${encodeURIComponent(version)}`}`,
  );
export const validateWorkflow = (workflow: Workflow) =>
  request<ValidationResult>(
    `/workflows/${encodeURIComponent(workflow.id)}/validate`,
    json(workflow),
  );
export const saveWorkflow = (workflow: Workflow) =>
  request<Workflow>(`/workflows/${encodeURIComponent(workflow.id)}`, {
    ...json(workflow),
    method: "PUT",
  });
/** 在既有工作流之外落一个全新的 id；后端在 id 已被占用时返回 409。 */
export const createWorkflow = (workflow: Workflow) =>
  request<Workflow>("/workflows", json(workflow));
export const runWorkflow = (id: string, inputs: Record<string, unknown>) =>
  request<RunResult>(
    `/workflows/${encodeURIComponent(id)}/runs`,
    json({ inputs }),
  );
export const getHealth = () =>
  request<{
    milvus: boolean;
    reranker_available?: boolean;
    reranker_loaded?: boolean;
    reranker_reason?: string;
    reranker_retry_in_seconds?: number;
  }>("/health");
export const uploadDocument = (file: File) => {
  const body = new FormData();
  body.append("file", file);
  return request<import("./documents").DocumentSubmission>("/documents", {
    method: "POST", body,
  });
};
