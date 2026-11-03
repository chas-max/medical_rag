import { object, PatientError } from "./client";

export type AuditRow = {
  id: string;
  time: string;
  operator_id: string | null;
  action: string;
  resource_type: string;
  outcome: string;
  reason_code: string;
  request_id: string;
};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function auditRows(value: unknown): AuditRow[] {
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
  return value.items.map((row: unknown) => {
    if (
      !object(row) ||
      typeof row.id !== "string" ||
      !uuid.test(row.id) ||
      typeof row.time !== "string" ||
      row.time.length > 64 ||
      !(
        row.operator_id === null ||
        (typeof row.operator_id === "string" && uuid.test(row.operator_id))
      ) ||
      typeof row.request_id !== "string" ||
      !uuid.test(row.request_id) ||
      !["action", "resource_type", "outcome", "reason_code"].every(
        (key) =>
          typeof row[key] === "string" && (row[key] as string).length <= 128,
      )
    )
      throw new PatientError();
    // Copy only the fixed metadata fields; never spread an arbitrary server body.
    return {
      id: row.id,
      time: row.time,
      operator_id: row.operator_id,
      action: row.action,
      resource_type: row.resource_type,
      outcome: row.outcome,
      reason_code: row.reason_code,
      request_id: row.request_id,
    } as AuditRow;
  });
}
