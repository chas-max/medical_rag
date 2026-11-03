import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AuditTable } from "../patient/AuditTable";
import { auditRows } from "../patient/audit";

afterEach(cleanup);
const row = {
  id: "00000000-0000-0000-0000-000000000001",
  time: "2026-11-02T00:00:00Z",
  operator_id: "00000000-0000-0000-0000-000000000002",
  action: "identity.read",
  resource_type: "identity",
  outcome: "allowed",
  reason_code: "ok",
  request_id: "00000000-0000-0000-0000-000000000003",
};
const page = { items: [row], limit: 20, offset: 0 };
it("copies only fixed metadata fields, with null operator allowed", () => {
  expect(
    auditRows({
      ...page,
      items: [{ ...row, private_body: "not-displayable" }],
    }),
  ).toEqual([row]);
  expect(
    auditRows({ ...page, items: [{ ...row, operator_id: null }] })[0]
      .operator_id,
  ).toBeNull();
});
it.each([
  null,
  {},
  { ...page, limit: 100 },
  { ...page, offset: -1 },
  { ...page, offset: 10001 },
  { ...page, offset: 0.5 },
  { ...page, items: Array(21).fill(row) },
  ...[
    "id",
    "time",
    "operator_id",
    "request_id",
    "action",
    "resource_type",
    "outcome",
    "reason_code",
  ].map((key) => ({ ...page, items: [{ ...row, [key]: 123 }] })),
  { ...page, items: [{ ...row, action: "x".repeat(129) }] },
])("rejects malformed audit metadata with static errors", (value) => {
  expect(() => auditRows(value)).toThrow("服务暂不可用，请重试。");
});
it("caps the audit UI at the API offset boundary and shows empty/loading states", () => {
  const { rerender } = render(
    <AuditTable rows={[row]} offset={10000} busy={false} page={vi.fn()} />,
  );
  expect(screen.getByRole("button", { name: "下一页审计" })).toBeDisabled();
  expect(screen.getByText(row.operator_id)).toBeInTheDocument();
  rerender(<AuditTable rows={[]} offset={0} busy={false} page={vi.fn()} />);
  expect(screen.getByText("没有更多审计事件。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "下一页审计" })).toBeDisabled();
  rerender(<AuditTable rows={null} offset={0} busy={false} page={vi.fn()} />);
  expect(screen.getByRole("status")).toHaveTextContent("正在读取审计元数据");
});
