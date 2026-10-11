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

export interface Workflow {
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

export interface RunResult {
  run_id: string;
  status: "success" | "failed";
  output: {
    answer?: string;
    citations?: Citation[];
    abstained?: boolean;
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

export type StreamEvent =
  | { event: "status"; data: StatusPayload }
  | { event: "claim"; data: ClaimPayload }
  | { event: "analysis"; data: AnalysisPayload }
  | { event: "done"; data: RunResult }
  | { event: "error"; data: { code: string; message: string; status?: number } };

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
export async function streamWorkflow(
  id: string,
  inputs: Record<string, unknown>,
  onEvent?: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<RunResult> {
  let response: Response;
  try {
    response = await fetch(
      `${base}/workflows/${encodeURIComponent(id)}/runs/stream`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        },
        body: JSON.stringify({ inputs }),
        signal,
      },
    );
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
    if (frame.event === "status") {
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

export const getNodes = () => request<NodeMetadata[]>("/nodes");
export const getWorkflows = () => request<Workflow[]>("/workflows");
export const getWorkflow = (id: string) =>
  request<Workflow>(`/workflows/${encodeURIComponent(id)}`);
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
export const runWorkflow = (id: string, inputs: Record<string, unknown>) =>
  request<RunResult>(
    `/workflows/${encodeURIComponent(id)}/runs`,
    json({ inputs }),
  );
export const getHealth = () => request<{ milvus: boolean }>("/health");
export const uploadDocument = (file: File) => {
  const body = new FormData();
  body.append("file", file);
  return request<{
    document_id: string;
    chunk_count: number;
    duplicate: boolean;
  }>("/documents", { method: "POST", body });
};
