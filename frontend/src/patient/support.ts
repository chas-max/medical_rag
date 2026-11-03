import { object, PatientError, request } from "./client";

export const categories = {
  login_help: "登录帮助",
  account_help: "账户帮助",
  usage_help: "使用帮助",
};
export const statuses = {
  open: "待受理",
  in_progress: "处理中",
  resolved: "已解决",
  cancelled: "已取消",
};
export const resolutions = {
  guidance_provided: "已提供指引",
  referred_to_institution: "已转介机构",
  not_reproducible: "未能复现",
};
export type Category = keyof typeof categories;
export type Resolution = keyof typeof resolutions;
export type SupportCase = {
  id: string;
  category: Category;
  status: keyof typeof statuses;
  resolution_code: Resolution | null;
  created_at: string;
  updated_at: string;
  requester_account_id?: string;
  assigned_account_id?: string | null;
};
const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
const date = (value: unknown): value is string =>
  typeof value === "string" &&
  value.length <= 40 &&
  /^\d{4}-\d\d-\d\dT/.test(value) &&
  Number.isFinite(Date.parse(value));
const member = (value: unknown, values: object) =>
  typeof value === "string" && Object.hasOwn(values, value);
export function supportCase(value: unknown, queue = false): SupportCase {
  if (
    !object(value) ||
    !uuid(value.id) ||
    !member(value.category, categories) ||
    !member(value.status, statuses) ||
    !(
      value.resolution_code === null ||
      member(value.resolution_code, resolutions)
    ) ||
    !date(value.created_at) ||
    !date(value.updated_at) ||
    (value.status === "resolved") !== (value.resolution_code !== null) ||
    (queue &&
      (!uuid(value.requester_account_id) ||
        !(
          value.assigned_account_id === null || uuid(value.assigned_account_id)
        ) ||
        (value.status === "open" && value.assigned_account_id !== null) ||
        (["in_progress", "resolved"].includes(value.status as string) &&
          value.assigned_account_id === null)))
  )
    throw new PatientError();
  return {
    id: value.id,
    category: value.category as Category,
    status: value.status as SupportCase["status"],
    resolution_code: value.resolution_code as Resolution | null,
    created_at: value.created_at,
    updated_at: value.updated_at,
    ...(queue
      ? {
          requester_account_id: value.requester_account_id as string,
          assigned_account_id: value.assigned_account_id as string | null,
        }
      : {}),
  };
}
export function supportPage(value: unknown, queue = false): SupportCase[] {
  if (
    !object(value) ||
    !Array.isArray(value.items) ||
    value.items.length > 20 ||
    value.limit !== 20 ||
    !Number.isInteger(value.offset) ||
    (value.offset as number) < 0 ||
    (value.offset as number) > 10000
  )
    throw new PatientError();
  return value.items.map((item) => supportCase(item, queue));
}
export async function listSupport(queue: boolean, offset = 0) {
  return supportPage(
    await request(
      `/support/${queue ? "queue" : "cases"}?limit=20&offset=${offset}`,
    ),
    queue,
  );
}
export async function changeSupport(
  id: string | null,
  operation: "create" | "cancel" | "take" | "resolve",
  csrf: string,
  value?: Category | Resolution,
) {
  const own = operation === "create" || operation === "cancel";
  const path =
    operation === "create"
      ? "/support/cases"
      : `/support/${own ? "cases" : "queue"}/${id}${own ? "" : `/${operation}`}`;
  const body =
    operation === "create"
      ? { category: value }
      : operation === "resolve"
        ? { resolution_code: value }
        : operation === "take"
          ? {}
          : undefined;
  return supportCase(
    await request(path, operation === "cancel" ? "DELETE" : "POST", body, csrf),
    !own,
  );
}
