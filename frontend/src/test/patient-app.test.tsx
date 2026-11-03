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

const identity = {
  user_id: "account-own",
  tenant_id: "institution-own",
  roles: ["patient"],
  binding: { verified: true },
  permissions: {
    identity: true,
    health_consent: true,
    conversations: true,
    audit: false,
    support_self: true,
    support_queue: false,
  },
};
const notice = {
  notice: {
    version: "institution-v7",
    text: "机构完整告知 <script>private</script>",
    validity_seconds: 3600,
  },
  granted: false,
  expires_at: null,
};
const item = {
  id: "00000000-0000-0000-0000-000000000001",
  title: "私密标题",
  created_at: "2026-01-01",
};
let handler: (url: string, init: RequestInit) => unknown;
let calls: string[];
const ok = (data: unknown) =>
  new Response(JSON.stringify({ success: true, data, error: null }), {
    headers: { "Content-Type": "application/json" },
  });
beforeEach(() => {
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  calls = [];
  window.history.replaceState(null, "", window.location.pathname);
  handler = (url) =>
    url === "/auth/session"
      ? { ...identity, csrf_token: "csrf-memory" }
      : url === "/me"
        ? identity
        : url.includes("consents")
          ? notice
          : url.includes("turns")
            ? {
                items: [
                  {
                    id: item.id,
                    content: "私密记录",
                    created_at: item.created_at,
                  },
                ],
                limit: 20,
                offset: 0,
              }
            : { items: [item], limit: 20, offset: 0 };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push(url);
      const result = await handler(url, init);
      return result instanceof Response ? result : ok(result);
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
const open = async (name: string) => {
  await screen.findByRole("button", { name });
  fireEvent.click(screen.getByRole("button", { name }));
};
const auditItem = {
  id: item.id,
  time: "2026-11-02T00:00:00Z",
  operator_id: null,
  action: "<img src=x onerror=alert(1)>",
  resource_type: "identity",
  outcome: "allowed",
  reason_code: "ok",
  request_id: "00000000-0000-0000-0000-000000000002",
};
const auditorIdentity = {
  ...identity,
  roles: ["auditor"],
  binding: { verified: false },
  permissions: {
    identity: true,
    health_consent: false,
    conversations: false,
    audit: true,
    support_self: true,
    support_queue: false,
  },
};
it("shows permission-gated audit metadata as plain text with bounded pagination", async () => {
  handler = (url) =>
    url === "/auth/session"
      ? { csrf_token: "csrf-memory" }
      : url === "/me"
        ? auditorIdentity
        : { items: [auditItem], limit: 20, offset: 0 };
  render(<PatientApp />);
  await open("安全审计");
  expect(
    await screen.findByRole("table", { name: "安全审计元数据" }),
  ).toBeInTheDocument();
  expect(screen.getByText(auditItem.action)).toBeInTheDocument();
  expect(document.querySelector("img")).toBeNull();
  expect(screen.getByText("未验证操作者")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "健康信息授权" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "会话记录" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "上一页审计" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "下一页审计" }));
  await waitFor(() => expect(calls).toContain("/audit?limit=20&offset=20"));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "上一页审计" }),
    ).not.toBeDisabled(),
  );
});
it.each(["patient", "support", "org_admin", "platform_operator", "auditor"])(
  "denies direct audit hash without fetching for %s lacking effective permission",
  async (role) => {
    window.history.replaceState(null, "", "#audit");
    handler = (url) =>
      url === "/auth/session"
        ? { csrf_token: "csrf" }
        : { ...identity, roles: [role] };
    render(<PatientApp />);
    expect(await screen.findByText(/403/)).toBeInTheDocument();
    expect(calls).toEqual(["/auth/session", "/me"]);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  },
);
it("discards delayed audit metadata on logout and keeps audit503 logout terminal", async () => {
  let resolveAudit!: (value: unknown) => void;
  handler = (url) =>
    url === "/auth/session"
      ? { csrf_token: "csrf" }
      : url === "/me"
        ? auditorIdentity
        : url === "/auth/logout"
          ? new Response("private-audit-error", { status: 503 })
          : new Promise((resolve) => {
              resolveAudit = resolve;
            });
  render(<PatientApp />);
  await open("安全审计");
  await waitFor(() => expect(calls).toContain("/audit?limit=20&offset=0"));
  fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
  expect(await screen.findByText("服务暂不可用，请重试。")).toBeInTheDocument();
  await act(async () =>
    resolveAudit({ items: [auditItem], limit: 20, offset: 0 }),
  );
  const before = calls.length;
  fireEvent(window, new Event("focus"));
  fireEvent(window, new Event("hashchange"));
  expect(calls).toHaveLength(before);
  expect(screen.queryByText(auditItem.action)).not.toBeInTheDocument();
});
it("shows only a same-origin institutional POST login after 401", async () => {
  handler = () => new Response("secret", { status: 401 });
  render(<PatientApp />);
  const login = await screen.findByRole("button", { name: "机构登录" });
  expect(login.closest("form")).toHaveAttribute("action", "/auth/login");
  expect(login.closest("form")).toHaveAttribute("method", "post");
  expect(calls).toEqual(["/auth/session"]);
});
it("loads identity, translates roles, keeps staff and unverified paths closed", async () => {
  handler = (url) =>
    url === "/auth/session"
      ? { ...identity, csrf_token: "csrf" }
      : {
          ...identity,
          roles: [
            "org_admin",
            "support",
            "platform_operator",
            "guardian",
            "unknown",
          ],
          binding: { verified: false },
          permissions: {
            identity: true,
            health_consent: false,
            conversations: false,
            audit: false,
            support_self: true,
            support_queue: false,
          },
        };
  render(<PatientApp />);
  expect(await screen.findByText("account-own")).toBeInTheDocument();
  expect(screen.getByText(/管理身份不授予/)).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "会话记录" }),
  ).not.toBeInTheDocument();
  expect(calls).toEqual(["/auth/session", "/me"]);
});
it("explicit forbidden hash shows 403 without fetching sensitive content", async () => {
  window.history.replaceState(null, "", "#conversations");
  handler = (url) =>
    url === "/auth/session"
      ? { ...identity, csrf_token: "csrf" }
      : {
          ...identity,
          permissions: {
            identity: true,
            health_consent: false,
            conversations: false,
            audit: false,
            support_self: true,
            support_queue: false,
          },
        };
  render(<PatientApp />);
  expect(await screen.findByText(/403/)).toBeInTheDocument();
  expect(calls).toEqual(["/auth/session", "/me"]);
});
it("requires unchecked acknowledgment and submits exact version+CSRF, then refreshes authority", async () => {
  render(<PatientApp />);
  await open("健康信息授权");
  expect(await screen.findByText(notice.notice.text)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "明确同意" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "明确同意" }));
  await waitFor(() => expect(calls.filter((x) => x === "/me")).toHaveLength(2));
  expect(fetch).toHaveBeenCalledWith(
    "/me/consents/health",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ version: "institution-v7", accepted: true }),
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": "csrf-memory",
      },
    }),
  );
  expect(document.querySelector("script")).toBeNull();
});
it("lists/selects/paginates, creates and appends only plain records; duplicates disabled", async () => {
  const original = handler;
  let appended = false;
  handler = (url, init) =>
    init.method === "POST"
      ? url.includes("turns")
        ? ((appended = true),
          {
            id: "00000000-0000-0000-0000-000000000002",
            content: "新记录",
            created_at: item.created_at,
          })
        : { ...item, id: "00000000-0000-0000-0000-000000000003" }
      : url.includes("turns") && appended
        ? {
            items: [
              { id: item.id, content: "新记录", created_at: item.created_at },
            ],
            limit: 20,
            offset: 0,
          }
        : original(url, init);
  render(<PatientApp />);
  await open("会话记录");
  await open("私密标题");
  expect(await screen.findByText("私密记录")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("记录内容"), {
    target: { value: "新记录" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存记录" }));
  await screen.findByText("新记录");
  fireEvent.change(screen.getByLabelText("会话标题"), {
    target: { value: "新标题" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建会话" }));
  await waitFor(() =>
    expect(fetch).toHaveBeenCalledWith(
      "/conversations",
      expect.objectContaining({ body: JSON.stringify({ title: "新标题" }) }),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "下一页会话" }));
  await waitFor(() =>
    expect(calls).toContain("/conversations?limit=20&offset=20"),
  );
  fireEvent.click(screen.getByRole("button", { name: "上一页会话" }));
  expect(window.location.href).not.toContain(item.id);
});
it("logout clears sensitive state synchronously and rejects delayed old replies", async () => {
  let resolve!: (value: unknown) => void;
  const original = handler;
  handler = (url, init) =>
    url.includes("turns")
      ? new Promise((r) => {
          resolve = r;
        })
      : original(url, init);
  render(<PatientApp />);
  await open("会话记录");
  await open("私密标题");
  await open("退出登录");
  expect(screen.queryByText("私密标题")).not.toBeInTheDocument();
  await act(async () =>
    resolve({
      items: [
        { id: item.id, content: "迟到敏感记录", created_at: item.created_at },
      ],
      limit: 20,
      offset: 0,
    }),
  );
  expect(screen.queryByText("迟到敏感记录")).not.toBeInTheDocument();
  await screen.findByRole("button", { name: "机构登录" });
});
it.each([401, 403, 503])(
  "clears on content authority failure %s with static errors",
  async (status) => {
    const original = handler;
    handler = (url, init) =>
      url.includes("turns")
        ? new Response("backend-secret", { status })
        : original(url, init);
    render(<PatientApp />);
    await open("会话记录");
    await open("私密标题");
    await waitFor(() =>
      expect(screen.queryByText("私密标题")).not.toBeInTheDocument(),
    );
    expect(document.body.textContent).not.toContain("backend-secret");
  },
);
it("withdraw clears before awaiting failed network and does not restore content", async () => {
  let resolve!: (value: unknown) => void;
  const original = handler;
  handler = (url, init) =>
    init.method === "DELETE"
      ? new Promise((r) => {
          resolve = r;
        })
      : url.includes("consents")
        ? { ...notice, granted: true }
        : original(url, init);
  render(<PatientApp />);
  await open("会话记录");
  await open("私密标题");
  await screen.findByText("私密记录");
  await open("健康信息授权");
  await open("撤回授权");
  expect(screen.queryByText("私密记录")).not.toBeInTheDocument();
  await act(async () => resolve(new Response("secret", { status: 503 })));
  expect(screen.queryByText("私密标题")).not.toBeInTheDocument();
});
it("blur clears drafts/content; focus reauthenticates before content", async () => {
  render(<PatientApp />);
  await open("会话记录");
  await screen.findByText("私密标题");
  fireEvent.change(screen.getByLabelText("会话标题"), {
    target: { value: "私密草稿" },
  });
  fireEvent.blur(window);
  expect(screen.queryByText("私密标题")).not.toBeInTheDocument();
  fireEvent.focus(window);
  await waitFor(() =>
    expect(calls.filter((x) => x === "/auth/session")).toHaveLength(2),
  );
  await screen.findByText("account-own");
  expect(screen.queryByDisplayValue("私密草稿")).not.toBeInTheDocument();
});
it("network unavailable offers safe retry and never persists patient data", async () => {
  const storage = vi.spyOn(Storage.prototype, "setItem");
  handler = () => Promise.reject(Error("secret"));
  render(<PatientApp />);
  await screen.findByText("服务暂不可用，请重试。");
  expect(storage).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
});
it("locks selection during mutation and reloads real server pages after create/append", async () => {
  let release!: (data: unknown) => void;
  const original = handler;
  let created = false,
    appended = false;
  handler = (url, init) => {
    if (init.method === "POST")
      return new Promise((resolve) => {
        release = resolve;
      });
    if (url.startsWith("/conversations?") && created)
      return {
        items: [{ ...item, title: "服务器第一页" }],
        limit: 20,
        offset: 0,
      };
    if (url.includes("turns") && appended)
      return {
        items: [
          { id: item.id, content: "服务器记录页", created_at: item.created_at },
        ],
        limit: 20,
        offset: 0,
      };
    return original(url, init);
  };
  render(<PatientApp />);
  await open("会话记录");
  await open("私密标题");
  await screen.findByText("私密记录");
  fireEvent.click(screen.getByRole("button", { name: "下一页记录" }));
  await waitFor(() =>
    expect(calls).toContain(
      `/conversations/${item.id}/turns?limit=20&offset=20`,
    ),
  );
  fireEvent.change(screen.getByLabelText("记录内容"), {
    target: { value: "new" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存记录" }));
  expect(screen.getByRole("button", { name: "私密标题" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "会话记录" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "保存记录" }));
  expect(calls.filter((x) => x.endsWith("/turns"))).toHaveLength(1);
  appended = true;
  await act(async () =>
    release({ id: item.id, content: "new", created_at: item.created_at }),
  );
  await screen.findByText("服务器记录页");
  expect(screen.getByRole("button", { name: "上一页记录" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "下一页会话" }));
  await waitFor(() =>
    expect(calls).toContain("/conversations?limit=20&offset=20"),
  );
  fireEvent.change(screen.getByLabelText("会话标题"), {
    target: { value: "new" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建会话" }));
  created = true;
  await act(async () => release({ ...item, title: "new" }));
  await screen.findByRole("button", { name: "服务器第一页" });
  expect(screen.getByRole("button", { name: "上一页会话" })).toBeDisabled();
  expect(screen.queryByText("new")).not.toBeInTheDocument();
});
it.each(["session", "identity", "notice", "page", "row"])(
  "rejects malformed %s data without echo",
  async (kind) => {
    const original = handler;
    handler = (url, init) =>
      kind === "session" && url === "/auth/session"
        ? {}
        : kind === "identity" && url === "/me"
          ? {}
          : kind === "notice" && url.includes("consents")
            ? {}
            : kind === "page" && url.startsWith("/conversations?")
              ? {}
              : kind === "row" && url.startsWith("/conversations?")
                ? {
                    items: [
                      { id: "secret", title: "secret", created_at: "secret" },
                    ],
                    limit: 20,
                    offset: 0,
                  }
                : original(url, init);
    render(<PatientApp />);
    if (kind === "notice") await open("健康信息授权");
    if (kind === "page" || kind === "row") await open("会话记录");
    await screen.findByText("服务暂不可用，请重试。");
    expect(document.body.textContent).not.toContain("secret");
  },
);
it("successful withdrawal reauthenticates and no longer offers conversations", async () => {
  const original = handler;
  let withdrawn = false;
  handler = (url, init) => {
    if (init.method === "DELETE") {
      withdrawn = true;
      return notice;
    }
    if (url === "/me" && withdrawn)
      return {
        ...identity,
        permissions: {
          identity: true,
          health_consent: true,
          conversations: false,
          audit: false,
          support_self: true,
          support_queue: false,
        },
      };
    if (url.includes("consents")) return { ...notice, granted: !withdrawn };
    return original(url, init);
  };
  render(<PatientApp />);
  await open("健康信息授权");
  await open("撤回授权");
  await screen.findByText("当前未授权");
  expect(
    screen.queryByRole("button", { name: "会话记录" }),
  ).not.toBeInTheDocument();
  await open("我的身份");
  expect(await screen.findByText("account-own")).toBeInTheDocument();
});
it("visibility hiding rejects late old page responses and rechecks on return", async () => {
  const original = handler;
  let release!: (data: unknown) => void;
  handler = (url, init) =>
    url.startsWith("/conversations?")
      ? new Promise((r) => (release = r))
      : original(url, init);
  render(<PatientApp />);
  await open("会话记录");
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "hidden",
  });
  fireEvent(document, new Event("visibilitychange"));
  await act(async () => release({ items: [item], limit: 20, offset: 0 }));
  expect(screen.queryByText("私密标题")).not.toBeInTheDocument();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    value: "visible",
  });
  fireEvent(document, new Event("visibilitychange"));
  await screen.findByText("account-own");
});
it("safe retry rechecks after unavailable response", async () => {
  const original = handler;
  handler = () => new Response("secret", { status: 503 });
  render(<PatientApp />);
  await screen.findByText("服务暂不可用，请重试。");
  handler = original;
  await open("重试");
  await screen.findByText("account-own");
});

it.each(["logout", "withdraw"] as const)(
  "%s survives focus/hash/visibility without private revalidation",
  async (kind) => {
    const original = handler;
    let release!: (value: unknown) => void;
    let withdrawn = false;
    handler = (url, init) => {
      if (
        (kind === "logout" && url === "/auth/logout") ||
        (kind === "withdraw" && init.method === "DELETE")
      )
        return new Promise((r) => (release = r));
      if (url === "/me" && withdrawn)
        return {
          ...identity,
          permissions: {
            identity: true,
            health_consent: true,
            conversations: false,
            audit: false,
            support_self: true,
            support_queue: false,
          },
        };
      if (url.includes("consents")) return { ...notice, granted: !withdrawn };
      return original(url, init);
    };
    render(<PatientApp />);
    await open("会话记录");
    await screen.findByText("私密标题");
    if (kind === "withdraw") {
      await open("健康信息授权");
      await open("撤回授权");
    } else await open("退出登录");
    const requests = calls.length;
    fireEvent.blur(window);
    fireEvent.focus(window);
    window.history.replaceState(null, "", "#conversations");
    fireEvent(window, new Event("hashchange"));
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    fireEvent(document, new Event("visibilitychange"));
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    fireEvent(document, new Event("visibilitychange"));
    await act(async () => {});
    expect(calls).toHaveLength(requests);
    expect(screen.queryByText("私密标题")).not.toBeInTheDocument();
    withdrawn = true;
    await act(async () => release(kind === "logout" ? null : notice));
    if (kind === "logout")
      await screen.findByRole("button", { name: "机构登录" });
    else await screen.findByText("当前未授权");
    expect(screen.queryByText("私密标题")).not.toBeInTheDocument();
  },
);

it.each(
  (["logout", "withdraw"] as const).flatMap((kind) =>
    ([401, 403, 503, "network", "malformed"] as const).flatMap((outcome) =>
      [false, true].map((inactive) => ({ kind, outcome, inactive })),
    ),
  ),
)(
  "failed $kind ($outcome, inactive=$inactive) stays locked until explicit retry",
  async ({ kind, outcome, inactive }) => {
    const original = handler;
    let release!: (value: unknown) => void;
    handler = (url, init) =>
      (kind === "logout" && url === "/auth/logout") ||
      (kind === "withdraw" && init.method === "DELETE")
        ? new Promise((r) => (release = r))
        : url.includes("consents")
          ? { ...notice, granted: true }
          : original(url, init);
    render(<PatientApp />);
    if (kind === "withdraw") {
      await open("健康信息授权");
      await open("撤回授权");
    } else await open("退出登录");
    const beforeReturn = calls.length;
    fireEvent.blur(window);
    if (inactive) {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "hidden",
      });
      fireEvent(document, new Event("visibilitychange"));
    } else fireEvent.focus(window);
    await act(async () =>
      release(
        outcome === "network"
          ? Promise.reject(Error("terminal-secret"))
          : outcome === "malformed"
            ? new Response("terminal-secret", {
                headers: { "Content-Type": "application/json" },
              })
            : new Response("terminal-secret", { status: outcome }),
      ),
    );
    if (inactive) {
      expect(screen.getByText(/页面已锁定/)).toBeInTheDocument();
      expect(calls).toHaveLength(beforeReturn);
      window.history.replaceState(null, "", "#conversations");
      fireEvent(window, new Event("hashchange"));
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      fireEvent(document, new Event("visibilitychange"));
      expect(calls).toHaveLength(beforeReturn);
      fireEvent.focus(window);
    }
    if (outcome === 401)
      await screen.findByRole("button", { name: "机构登录" });
    else if (outcome === 403) await screen.findByText(/403/);
    else await screen.findByText("服务暂不可用，请重试。");
    const requests = calls.length;
    fireEvent.blur(window);
    fireEvent.focus(window);
    window.history.replaceState(null, "", "#conversations");
    fireEvent(window, new Event("hashchange"));
    await act(async () => {});
    expect(calls).toHaveLength(requests);
    expect(screen.queryByText("account-own")).not.toBeInTheDocument();
    expect(screen.queryByText("私密标题")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("terminal-secret");
    if (outcome !== 401) {
      await open(outcome === 403 ? "重新验证身份" : "重试");
      await screen.findByText("account-own");
    }
  },
);

it.each(["logout", "withdraw"] as const)(
  "inactive completion of %s stays cleared until return",
  async (kind) => {
    const original = handler;
    let release!: (value: unknown) => void;
    let withdrawn = false;
    handler = (url, init) => {
      if (
        (kind === "logout" && url === "/auth/logout") ||
        (kind === "withdraw" && init.method === "DELETE")
      )
        return new Promise((r) => (release = r));
      if (url === "/me" && withdrawn)
        return {
          ...identity,
          permissions: {
            identity: true,
            health_consent: true,
            conversations: false,
            audit: false,
            support_self: true,
            support_queue: false,
          },
        };
      return url.includes("consents")
        ? { ...notice, granted: !withdrawn }
        : original(url, init);
    };
    render(<PatientApp />);
    if (kind === "withdraw") {
      await open("健康信息授权");
      await open("撤回授权");
    } else await open("退出登录");
    fireEvent.blur(window);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    fireEvent(document, new Event("visibilitychange"));
    const requests = calls.length;
    withdrawn = true;
    await act(async () => release(kind === "logout" ? null : notice));
    expect(calls).toHaveLength(requests);
    expect(screen.queryByText("account-own")).not.toBeInTheDocument();
    expect(screen.getByText(/页面已锁定/)).toBeInTheDocument();
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    fireEvent(document, new Event("visibilitychange"));
    expect(calls).toHaveLength(requests);
    fireEvent.focus(window);
    if (kind === "logout")
      await screen.findByRole("button", { name: "机构登录" });
    else await screen.findByText("当前未授权");
  },
);

it.each(["initial-turns", "page-turns", "initial-list", "page-list"])(
  "old %s cannot overwrite post-mutation page",
  async (kind) => {
    const original = handler;
    let release!: (value: unknown) => void;
    let saved = false,
      initialHeld = false;
    const turn = kind.includes("turns");
    handler = (url, init) => {
      if (init.method === "POST") {
        saved = true;
        return turn
          ? { id: item.id, content: "新保存记录", created_at: item.created_at }
          : { ...item, title: "新保存标题" };
      }
      const relevant = turn
        ? url.includes("turns?")
        : url.startsWith("/conversations?");
      if (relevant && saved)
        return {
          items: [
            turn
              ? {
                  id: item.id,
                  content: "新保存记录",
                  created_at: item.created_at,
                }
              : { ...item, title: "新保存标题" },
          ],
          limit: 20,
          offset: 0,
        };
      if (
        relevant &&
        ((kind.startsWith("initial") && !initialHeld) ||
          url.endsWith("offset=20"))
      ) {
        initialHeld = true;
        return new Promise((r) => (release = r));
      }
      return original(url, init);
    };
    render(<PatientApp />);
    await open("会话记录");
    if (turn) await open("私密标题");
    if (kind === "page-turns") {
      await screen.findByText("私密记录");
      await open("下一页记录");
    }
    if (kind === "page-list") {
      await screen.findByText("私密标题");
      await open("下一页会话");
    }
    fireEvent.change(screen.getByLabelText(turn ? "记录内容" : "会话标题"), {
      target: { value: "new" },
    });
    await open(turn ? "保存记录" : "创建会话");
    await screen.findByText(turn ? "新保存记录" : "新保存标题");
    await act(async () =>
      release({
        items: [
          turn
            ? { id: item.id, content: "旧响应", created_at: item.created_at }
            : { ...item, title: "旧响应" },
        ],
        limit: 20,
        offset: kind.startsWith("page") ? 20 : 0,
      }),
    );
    expect(
      screen.getByText(turn ? "新保存记录" : "新保存标题"),
    ).toBeInTheDocument();
    expect(screen.queryByText("旧响应")).not.toBeInTheDocument();
  },
);

it("visibility alone cannot unlock an unfocused window", async () => {
  render(<PatientApp />);
  await screen.findByText("account-own");
  fireEvent.blur(window);
  const count = calls.length;
  fireEvent(document, new Event("visibilitychange"));
  await act(async () => {});
  expect(calls).toHaveLength(count);
  expect(screen.queryByText("account-own")).not.toBeInTheDocument();
  fireEvent.focus(window);
  await screen.findByText("account-own");
});
it("initially unfocused window waits for actual focus", async () => {
  vi.mocked(document.hasFocus).mockReturnValue(false);
  render(<PatientApp />);
  await act(async () => {});
  expect(calls).toEqual([]);
  expect(screen.getByText(/页面已锁定/)).toBeInTheDocument();
  fireEvent.focus(window);
  await screen.findByText("account-own");
});
