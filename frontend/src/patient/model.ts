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
export type Row = {
  id: string;
  title?: string;
  content?: string;
  created_at: string;
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
export function row(value: unknown, field: "title" | "content"): Row {
  if (
    !object(value) ||
    typeof value.id !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      value.id,
    ) ||
    typeof value[field] !== "string" ||
    typeof value.created_at !== "string"
  )
    throw new PatientError();
  return value as Row;
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
