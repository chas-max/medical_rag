export interface NodeMetadata {
  type: string;
  title: string;
  category: string;
  inputs: Record<string, string>;
  outputs: Record<string, string>;
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

export interface WorkflowNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  config: Record<string, string | number | boolean | null>;
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

const base = import.meta.env.DEV ? "/api" : "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, init);
  } catch {
    throw new Error("无法连接服务，请确认后端已经启动");
  }
  let envelope: {
    success: boolean;
    data: T;
    error: { message: string } | null;
  };
  try {
    envelope = await response.json();
  } catch {
    throw new Error("服务返回了无法解析的响应");
  }
  if (!response.ok || !envelope.success)
    throw new Error(envelope.error?.message || "请求失败");
  return envelope.data;
}

const json = (value: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
});

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
