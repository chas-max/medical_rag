import { object, PatientError } from "./client";

export type View =
  "identity" | "consent" | "conversations" | "audit" | "support";
export type Identity = {
  user_id: string;
  tenant_id: string;
  roles: string[];
  binding: { verified: boolean };
  permissions: {
    identity: boolean;
    health_consent: boolean;
    conversations: boolean;
    chat: boolean;
    /** Absent on a server that predates streaming; treat as unsupported then. */
    chat_stream?: boolean;
    audit: boolean;
    support_self: boolean;
    support_queue: boolean;
  };
};
export type Consent = {
  notice: { version: string; text: string; validity_seconds: number };
  granted: boolean;
  expires_at: string | null;
};
export type Citation = {
  chunk_id: string;
  filename: string;
  section: string;
  page?: number;
  origin?: string;
  excerpt?: string;
};
export type Row = {
  id: string;
  title?: string;
  content?: string;
  role: "user" | "assistant";
  citations: Citation[];
  /** 服务端权威回答类型；旧记录缺失时按普通回答解释。 */
  answer_type: "answer" | "clarification";
  created_at: string;
};
export type ChatResult = {
  conversation: Row;
  answer: Row;
  abstained: boolean;
};
export const roles: Record<string, string> = {
  patient: "患者",
  org_admin: "机构管理员",
  support: "机构客服",
  platform_operator: "平台运维",
  knowledge_admin: "知识管理员",
  integration_admin: "集成管理员",
  auditor: "审计员",
  guardian: "监护人",
};

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function citation(value: unknown): Citation {
  if (
    !object(value) ||
    typeof value.chunk_id !== "string" ||
    !value.chunk_id ||
    value.chunk_id.length > 256 ||
    typeof value.filename !== "string" ||
    !value.filename ||
    value.filename.length > 512 ||
    typeof value.section !== "string" ||
    value.section.length > 512
  )
    throw new PatientError();
  const result: Citation = {
    chunk_id: value.chunk_id,
    filename: value.filename,
    section: value.section,
  };
  if (value.page !== undefined) {
    if (
      typeof value.page !== "number" ||
      !Number.isInteger(value.page) ||
      value.page < 0 ||
      value.page > 100000
    )
      throw new PatientError();
    result.page = value.page;
  }
  if (value.origin !== undefined) {
    if (typeof value.origin !== "string" || !value.origin || value.origin.length > 32)
      throw new PatientError();
    result.origin = value.origin;
  }
  if (value.excerpt !== undefined) {
    if (typeof value.excerpt !== "string" || value.excerpt.length > 2000)
      throw new PatientError();
    result.excerpt = value.excerpt;
  }
  return result;
}

function citations(value: unknown): Citation[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 50) throw new PatientError();
  return value.map(citation);
}

export function identity(value: unknown): Identity {
  if (
    !object(value) ||
    typeof value.user_id !== "string" ||
    typeof value.tenant_id !== "string" ||
    !Array.isArray(value.roles) ||
    !value.roles.every((x) => typeof x === "string") ||
    !object(value.binding) ||
    typeof value.binding.verified !== "boolean" ||
    !object(value.permissions) ||
    value.permissions.identity !== true ||
    typeof value.permissions.health_consent !== "boolean" ||
    typeof value.permissions.conversations !== "boolean" ||
    typeof value.permissions.chat !== "boolean" ||
    (value.permissions.chat_stream !== undefined &&
      typeof value.permissions.chat_stream !== "boolean") ||
    typeof value.permissions.audit !== "boolean" ||
    typeof value.permissions.support_self !== "boolean" ||
    typeof value.permissions.support_queue !== "boolean"
  )
    throw new PatientError();
  return value as Identity;
}
export function consent(value: unknown): Consent {
  if (
    !object(value) ||
    !object(value.notice) ||
    typeof value.notice.version !== "string" ||
    typeof value.notice.text !== "string" ||
    typeof value.notice.validity_seconds !== "number" ||
    typeof value.granted !== "boolean" ||
    !(value.expires_at === null || typeof value.expires_at === "string")
  )
    throw new PatientError();
  return value as Consent;
}
function answerType(value: unknown): "answer" | "clarification" {
  if (value === undefined || value === "answer") return "answer";
  if (value === "clarification") return value;
  throw new PatientError();
}

export function row(value: unknown, field: "title" | "content"): Row {
  if (
    !object(value) ||
    typeof value.id !== "string" ||
    !uuid.test(value.id) ||
    typeof value[field] !== "string" ||
    typeof value.created_at !== "string"
  )
    throw new PatientError();
  const role = value.role === undefined ? "user" : value.role;
  if (role !== "user" && role !== "assistant") throw new PatientError();
  const result: Row = {
    id: value.id,
    role,
    citations: citations(value.citations),
    answer_type: answerType(value.answer_type),
    created_at: value.created_at,
  };
  if (field === "title") result.title = value[field] as string;
  else result.content = value[field] as string;
  return result;
}
export function rows(value: unknown, field: "title" | "content"): Row[] {
  if (
    !object(value) ||
    !Array.isArray(value.items) ||
    value.items.length > 20 ||
    typeof value.offset !== "number" ||
    value.limit !== 20
  )
    throw new PatientError();
  return value.items.map((x) => row(x, field));
}
export function chatResult(value: unknown): ChatResult {
  if (
    !object(value) ||
    !("conversation" in value) ||
    !("answer" in value) ||
    typeof value.abstained !== "boolean"
  )
    throw new PatientError();
  const conversation = row(value.conversation, "title");
  const answer = row(value.answer, "content");
  if (answer.role !== "assistant" || conversation.title === undefined)
    throw new PatientError();
  return { conversation, answer, abstained: value.abstained };
}
