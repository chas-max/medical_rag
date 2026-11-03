import type { AuditRow } from "./audit";

export function AuditTable({
  rows,
  offset,
  busy,
  page,
}: {
  rows: AuditRow[] | null;
  offset: number;
  busy: boolean;
  page: (amount: number) => void;
}) {
  return (
    <section>
      <h2>安全审计</h2>
      <p>
        仅显示本机构操作元数据，不包含患者标识、会话标识或医疗内容。此开发环境不代表合规验收。
      </p>
      {rows === null ? (
        <p role="status">正在读取审计元数据…</p>
      ) : (
        <>
          <div className="patient-audit">
            <table aria-label="安全审计元数据">
              <thead>
                <tr>
                  {[
                    "事件标识",
                    "服务器时间",
                    "操作者",
                    "操作",
                    "资源类型",
                    "结果",
                    "原因码",
                    "请求标识",
                  ].map((label) => (
                    <th scope="col" key={label}>
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td>{row.id}</td>
                    <td>{row.time}</td>
                    <td>{row.operator_id ?? "未验证操作者"}</td>
                    <td>{row.action}</td>
                    <td>{row.resource_type}</td>
                    <td>{row.outcome}</td>
                    <td>{row.reason_code}</td>
                    <td>{row.request_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rows.length === 0 && <p>没有更多审计事件。</p>}
          </div>
          <div className="patient-paging">
            <button disabled={busy || offset === 0} onClick={() => page(-20)}>
              上一页审计
            </button>
            <span>第 {offset / 20 + 1} 页</span>
            <button
              disabled={busy || rows.length === 0 || offset >= 10000}
              onClick={() => page(20)}
            >
              下一页审计
            </button>
          </div>
        </>
      )}
    </section>
  );
}
