import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PatientApp } from "../patient/PatientApp";

const id = "00000000-0000-0000-0000-000000000001";
const me = {
  user_id: id,
  tenant_id: id,
  roles: ["support"],
  binding: { verified: false },
  permissions: {
    identity: true,
    health_consent: false,
    conversations: false,
    audit: false,
    support_self: true,
    support_queue: true,
  },
};
let state = "open";
let resolvePending: ((value: unknown) => void) | undefined;
let handler: (url: string, init: RequestInit) => unknown;
const row = () => ({
  id,
  category: "login_help",
  status: state,
  resolution_code: state === "resolved" ? "guidance_provided" : null,
  created_at: "2026-11-03T00:00:00Z",
  updated_at: "2026-11-03T00:00:00Z",
});
const page = (queue = false) => ({
  items: [
    queue
      ? {
          ...row(),
          requester_account_id: id,
          assigned_account_id: state === "open" ? null : id,
        }
      : row(),
  ],
  limit: 20,
  offset: 0,
});
beforeEach(() => {
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  window.history.replaceState(null, "", window.location.pathname + "#support");
  state = "open";
  resolvePending = undefined;
  handler = (url, init) => {
    if (url === "/auth/session") return { csrf_token: "memory-only-proof" };
    if (url === "/me") return me;
    if (init.method === "POST" && url.endsWith("/take")) {
      state = "in_progress";
      return { ...row(), requester_account_id: id, assigned_account_id: id };
    }
    if (init.method === "POST" && url.endsWith("/resolve")) {
      state = "resolved";
      return { ...row(), requester_account_id: id, assigned_account_id: id };
    }
    if (init.method === "DELETE") {
      state = "cancelled";
      return row();
    }
    if (url === "/support/cases" && init.method === "POST") {
      state = "open";
      return row();
    }
    if (url.includes("?")) return page(url.includes("queue"));
    return row();
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      const data = await handler(url, init);
      return data instanceof Response
        ? data
        : new Response(JSON.stringify({ success: true, data, error: null }), {
            headers: { "Content-Type": "application/json" },
          });
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
it("opens structured owner support and institution queue with functional transitions", async () => {
  render(<PatientApp />);
  expect(
    await screen.findByRole("heading", { name: "账户支持" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "创建支持请求" }));
  fireEvent.click(await screen.findByRole("button", { name: "查看详情" }));
  expect(
    await screen.findByRole("heading", { name: "请求详情" }),
  ).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "受理" }));
  fireEvent.click(await screen.findByRole("button", { name: "完成处理" }));
  expect(await screen.findAllByText(/已解决/)).not.toHaveLength(0);
});
it.each(["cases", "queue"])(
  "reselecting active support preserves its pending initial %s read",
  async (pendingList) => {
    const original = handler;
    let finishRead: ((value: unknown) => void) | undefined;
    handler = (url, init) =>
      url === `/support/${pendingList}?limit=20&offset=0`
        ? new Promise((resolve) => {
            finishRead = resolve;
          })
        : original(url, init);
    render(<PatientApp />);
    await screen.findByRole("heading", { name: "账户支持" });
    await waitFor(() => expect(finishRead).toBeDefined());
    expect(screen.getAllByText("正在读取支持请求…")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "账户支持" }));
    fireEvent.click(screen.getByRole("button", { name: "账户支持" }));
    await act(async () => {
      handler = original;
      finishRead?.(page(pendingList === "queue"));
    });

    fireEvent.click(await screen.findByRole("button", { name: "查看详情" }));
    expect(
      await screen.findByRole("heading", { name: "请求详情" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("正在读取支持请求…")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "受理" }));
    expect(
      await screen.findByRole("button", { name: "完成处理" }),
    ).toBeInTheDocument();
  },
);
it("owner cancellation is retained and blur clears all support data", async () => {
  render(<PatientApp />);
  fireEvent.click(await screen.findByRole("button", { name: "取消请求" }));
  expect(await screen.findAllByText(/已取消/)).not.toHaveLength(0);
  act(() => window.dispatchEvent(new Event("blur")));
  expect(
    screen.queryByRole("heading", { name: "账户支持" }),
  ).not.toBeInTheDocument();
});
it("direct unauthorized support view is forbidden", async () => {
  handler = (url) =>
    url === "/auth/session"
      ? { csrf_token: "memory-only-proof" }
      : {
          ...me,
          permissions: {
            ...me.permissions,
            support_self: false,
            support_queue: false,
          },
        };
  render(<PatientApp />);
  expect(await screen.findByRole("alert")).toHaveTextContent("403");
});
it("failed logout barrier prevents hash or focus from restoring support", async () => {
  render(<PatientApp />);
  await screen.findByRole("heading", { name: "账户支持" });
  const original = handler;
  handler = (url, init) =>
    url === "/auth/logout"
      ? new Promise((resolve) => {
          resolvePending = resolve;
        })
      : original(url, init);
  fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
  act(() => {
    window.dispatchEvent(new Event("blur"));
    window.dispatchEvent(new Event("focus"));
    window.dispatchEvent(new Event("hashchange"));
  });
  await act(async () => {
    resolvePending?.(new Response(null, { status: 503 }));
  });
  act(() => window.dispatchEvent(new Event("hashchange")));
  expect(
    screen.queryByRole("heading", { name: "账户支持" }),
  ).not.toBeInTheDocument();
  expect(await screen.findByRole("alert")).toHaveTextContent("暂不可用");
});

it('owner-only support and pagination use bounded routes, fixed selectors, detail and empty states',async()=>{
  const original=handler;
  handler=(url,init)=>url==='/me'?{...me,roles:['guardian'],permissions:{...me.permissions,support_queue:false}}:
    url.includes('offset=20')?{items:[],limit:20,offset:20}:original(url,init);
  render(<PatientApp/>);
  fireEvent.change(await screen.findByLabelText('问题类别'),{target:{value:'usage_help'}});
  expect(screen.queryByRole('heading',{name:'机构客服队列'})).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole('button',{name:'下一页请求'}));
  expect(await screen.findByText('暂无请求')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'上一页请求'}));
  fireEvent.click(await screen.findByRole('button',{name:'查看详情'}));
  expect(await screen.findByRole('heading',{name:'请求详情'})).toBeInTheDocument();
});
it('stale detail read cannot restore old state after cancellation',async()=>{
  render(<PatientApp/>);
  await screen.findByRole('button',{name:'查看详情'});
  const original=handler;
  handler=(url,init)=>url==='/support/cases/'+id&&init.method==='GET'?new Promise(resolve=>{resolvePending=resolve;}):original(url,init);
  fireEvent.click(screen.getByRole('button',{name:'查看详情'}));
  fireEvent.click(screen.getByRole('button',{name:'取消请求'}));
  await screen.findAllByText(/已取消/);
  await act(async()=>{resolvePending?.({...row(),status:'open'});});
  expect(screen.queryByRole('heading',{name:'请求详情'})).not.toBeInTheDocument();
  expect(screen.queryByText(/待受理/)).not.toBeInTheDocument();
});
it('queue pagination or revoked authority clears the view and invalidates stale responses',async()=>{
  render(<PatientApp/>);
  await screen.findByRole('button',{name:'下一页队列'});
  const original=handler;
  handler=(url,init)=>url.includes('queue?')?new Response(null,{status:403}):original(url,init);
  fireEvent.change(screen.getByLabelText('处理结果'),{target:{value:'not_reproducible'}});
  fireEvent.click(screen.getByRole('button',{name:'下一页队列'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('403');
  expect(screen.queryByRole('heading',{name:'账户支持'})).not.toBeInTheDocument();
});
