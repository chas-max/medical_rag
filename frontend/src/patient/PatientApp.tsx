import { useCallback, useEffect, useRef, useState } from "react";
import { object, PatientError, request } from "./client";
import { auditRows, type AuditRow } from "./audit";
import { AuditTable } from "./AuditTable";
import { SupportCases } from "./SupportCases";

import {
  identity,
  consent,
  row,
  rows,
  roles,
  type View,
  type Identity,
  type Consent,
  type Row,
} from "./model";
type Terminal = {
  kind: "logout" | "withdraw";
  status: "pending" | "login" | "forbidden" | "unavailable" | "withdrawn";
};
function failureStatus(error: unknown) {
  return error instanceof PatientError && error.status === 401
    ? "login"
    : error instanceof PatientError && error.status === 403
      ? "forbidden"
      : "unavailable";
}
export function PatientApp() {
  const [status, setStatus] = useState("loading"),
    [me, setMe] = useState<Identity | null>(null),
    [view, setView] = useState<View>("identity");
  const [notice, setNotice] = useState<Consent | null>(null),
    [accepted, setAccepted] = useState(false),
    [list, setList] = useState<Row[]>([]),
    [turns, setTurns] = useState<Row[]>([]);
  const [audit, setAudit] = useState<AuditRow[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null),
    [title, setTitle] = useState(""),
    [content, setContent] = useState(""),
    [offset, setOffset] = useState(0),
    [turnOffset, setTurnOffset] = useState(0),
    [busy, setBusy] = useState(false);
  const generation = useRef(0),
    csrf = useRef<string | null>(null),
    pending = useRef(false);
  // Terminal actions belong to a separate lifecycle: hiding/navigating may
  // invalidate view requests, but must never cancel logout/withdraw outcomes.
  const terminal = useRef<Terminal | null>(null),
    focused = useRef(document.hasFocus()),
    active = useRef(
      document.visibilityState !== "hidden" && document.hasFocus(),
    ),
    mounted = useRef(true);
  const clear = useCallback(() => {
    generation.current++;
    csrf.current = null;
    pending.current = false;
    setMe(null);
    setNotice(null);
    setAccepted(false);
    setList([]);
    setTurns([]);
    setAudit(null);
    setSelected(null);
    setTitle("");
    setContent("");
    setOffset(0);
    setTurnOffset(0);
    setBusy(false);
  }, []);
  const failure = useCallback(
    (error: unknown) => {
      clear();
      setStatus(active.current ? failureStatus(error) : "hidden");
    },
    [clear],
  );
  const perform = useCallback(
    async (operation: (live: () => boolean) => Promise<void>) => {
      const current = generation.current;
      const live = () =>
        generation.current === current &&
        active.current &&
        terminal.current === null;
      try {
        await operation(live);
      } catch (error) {
        if (live()) failure(error);
      }
    },
    [failure],
  );
  const refresh = useCallback(
    (target: View = "identity", explicit = false) => {
      if (!active.current) {
        clear();
        setStatus("hidden");
        return;
      }
      const barrier = terminal.current;
      if (barrier) {
        if (barrier.status === "pending") {
          setStatus("loading");
          return;
        }
        if (!explicit && barrier.status !== "withdrawn") {
          setStatus(barrier.status);
          return;
        }
        if (!explicit) target = "consent";
        terminal.current = null;
      }
      clear();
      setStatus("loading");
      void perform(async (live) => {
        const session = await request("/auth/session");
        if (!live()) return;
        if (
          !object(session) ||
          typeof session.csrf_token !== "string" ||
          !session.csrf_token
        )
          throw new PatientError();
        const own = identity(await request("/me"));
        if (!live()) return;
        csrf.current = session.csrf_token;
        setMe(own);
        setView(target);
        setStatus("ready");
        if (
          target !== "identity" &&
          !(target === "consent"
            ? own.permissions.health_consent
            : target === "audit"
              ? own.permissions.audit
              : target === "support"
                ? own.permissions.support_self
                : own.permissions.conversations)
        ) {
          clear();
          setStatus("forbidden");
          return;
        }
        if (target === "consent") {
          const data = consent(await request("/me/consents/health"));
          if (live()) setNotice(data);
        }
        if (target === "conversations") {
          const data = rows(
            await request("/conversations?limit=20&offset=0"),
            "title",
          );
          if (live()) setList(data);
        }
        if (target === "audit") {
          const data = auditRows(await request("/audit?limit=20&offset=0"));
          if (live()) setAudit(data);
        }
      });
    },
    [clear, perform],
  );
  useEffect(() => {
    mounted.current = true;
    focused.current = document.hasFocus();
    active.current = document.visibilityState !== "hidden" && focused.current;
    const requested = () =>
      window.location.hash === "#consent"
        ? "consent"
        : window.location.hash === "#audit"
          ? "audit"
          : window.location.hash === "#conversations"
            ? "conversations"
            : window.location.hash === "#support"
              ? "support"
              : "identity";
    refresh(requested());
    const hide = () => {
      active.current = false;
      clear();
      setStatus("hidden");
    };
    const focus = () => {
      focused.current = true;
      if (document.visibilityState !== "hidden") {
        active.current = true;
        refresh();
      }
    };
    const visibility = () => {
      if (document.visibilityState === "hidden") hide();
      else if (focused.current && document.hasFocus()) focus();
    };
    const blur = () => {
      focused.current = false;
      hide();
    };
    const hash = () => refresh(requested());
    window.addEventListener("blur", blur);
    window.addEventListener("focus", focus);
    window.addEventListener("hashchange", hash);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      mounted.current = false;
      generation.current++;
      window.removeEventListener("blur", blur);
      window.removeEventListener("focus", focus);
      window.removeEventListener("hashchange", hash);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [clear, refresh]);
  const navigate = (next: View) => {
    if (terminal.current || pending.current || !active.current) return;
    if (
      !me ||
      (next === "consent" && !me.permissions.health_consent) ||
      (next === "conversations" && !me.permissions.conversations) ||
      (next === "audit" && !me.permissions.audit) ||
      (next === "support" && !me.permissions.support_self)
    ) {
      failure(new PatientError(403));
      return;
    }
    // Support owns its loader; reselecting the mounted view must not discard
    // its pending reads without starting a replacement.
    if (next === "support" && view === "support") return;
    generation.current++;
    setView(next);
    setList([]);
    setAudit(null);
    setTurns([]);
    setSelected(null);
    setTitle("");
    setContent("");
    setNotice(null);
    setAccepted(false);
    setOffset(0);
    setTurnOffset(0);
    pending.current = false;
    setBusy(false);
    void perform(async (live) => {
      if (next === "consent") {
        const data = consent(await request("/me/consents/health"));
        if (live()) setNotice(data);
      }
      if (next === "conversations") {
        const data = rows(
          await request("/conversations?limit=20&offset=0"),
          "title",
        );
        if (live()) setList(data);
      }
      if (next === "audit") {
        const data = auditRows(await request("/audit?limit=20&offset=0"));
        if (live()) setAudit(data);
      }
    });
  };
  const mutate = (
    operation: (token: string, live: () => boolean) => Promise<void>,
  ) => {
    if (pending.current || !csrf.current || terminal.current || !active.current)
      return;
    // Invalidates initial/pagination reads before a mutation and its canonical
    // server reload; older reads cannot overwrite the newly saved page.
    generation.current++;
    pending.current = true;
    setBusy(true);
    const token = csrf.current;
    void perform(async (live) => {
      try {
        await operation(token, live);
      } finally {
        if (live()) {
          pending.current = false;
          setBusy(false);
        }
      }
    });
  };
  const supportRead = useCallback(
    (operation: (live: () => boolean) => Promise<void>) => {
      if (pending.current || terminal.current || !active.current) return;
      generation.current++;
      void perform(operation);
    },
    [perform],
  );
  const terminate = (kind: "logout" | "withdraw") => {
    if (terminal.current || !csrf.current || !active.current) return;
    const token = csrf.current;
    const barrier: Terminal = { kind, status: "pending" };
    terminal.current = barrier;
    clear();
    setStatus("loading");
    void (async () => {
      try {
        await request(
          kind === "logout" ? "/auth/logout" : "/me/consents/health",
          kind === "logout" ? "POST" : "DELETE",
          undefined,
          token,
        );
        barrier.status = kind === "logout" ? "login" : "withdrawn";
      } catch (error) {
        barrier.status = failureStatus(error);
      }
      if (!mounted.current || terminal.current !== barrier) return;
      clear();
      if (!active.current) setStatus("hidden");
      else if (barrier.status === "withdrawn") refresh("consent");
      else setStatus(barrier.status);
    })();
  };
  const logout = () => terminate("logout");
  const withdraw = () => terminate("withdraw");
  const pageAudit = (amount: number) => {
    if (pending.current || terminal.current || !active.current) return;
    generation.current++;
    const next = Math.min(10000, Math.max(0, offset + amount));
    void perform(async (live) => {
      const data = auditRows(await request(`/audit?limit=20&offset=${next}`));
      if (live()) {
        setAudit(data);
        setOffset(next);
      }
    });
  };
  const page = (amount: number, isTurn = false) => {
    if (pending.current || terminal.current || !active.current) return;
    generation.current++;
    const next = Math.min(
      10000,
      Math.max(0, (isTurn ? turnOffset : offset) + amount),
    );
    void perform(async (live) => {
      const data = rows(
        await request(
          isTurn
            ? `/conversations/${selected}/turns?limit=20&offset=${next}`
            : `/conversations?limit=20&offset=${next}`,
        ),
        isTurn ? "content" : "title",
      );
      if (live()) {
        if (isTurn) {
          setTurns(data);
          setTurnOffset(next);
        } else {
          setList(data);
          setOffset(next);
        }
      }
    });
  };
  return (
    <div className="patient-shell">
      <header>
        <div className="patient-mark">机构服务 · 患者门户</div>
        <h1>身份与会话记录</h1>
        <p>虚构数据开发环境 · 不提供诊断或模型生成</p>
      </header>
      <main>
        <p className="patient-note">
          离开或隐藏页面会清空本页敏感内容；返回时重新验证身份与权限。闲置期间不提供即时推送撤权。
        </p>
        {status === "loading" && <p role="status">正在安全验证…</p>}
        {status === "hidden" && (
          <p role="status">页面已锁定，返回后重新验证。</p>
        )}
        {status === "login" && (
          <section>
            <h2>机构身份验证</h2>
            <form action="/auth/login" method="post">
              <button type="submit">机构登录</button>
            </form>
          </section>
        )}
        {status === "unavailable" && (
          <section>
            <p role="alert">服务暂不可用，请重试。</p>
            <button onClick={() => refresh("identity", true)}>重试</button>
          </section>
        )}
        {status === "forbidden" && (
          <section>
            <p role="alert">403 · 当前权限不允许访问此内容。</p>
            <button onClick={() => refresh("identity", true)}>
              重新验证身份
            </button>
          </section>
        )}
        {status === "ready" && me && (
          <>
            <nav aria-label="服务导航">
              <button disabled={busy} onClick={() => navigate("identity")}>
                我的身份
              </button>
              {me.permissions.health_consent && (
                <button disabled={busy} onClick={() => navigate("consent")}>
                  健康信息授权
                </button>
              )}
              {me.permissions.conversations && (
                <button
                  disabled={busy}
                  onClick={() => navigate("conversations")}
                >
                  会话记录
                </button>
              )}
              {me.permissions.audit && (
                <button disabled={busy} onClick={() => navigate("audit")}>
                  安全审计
                </button>
              )}
              {me.permissions.support_self && (
                <button disabled={busy} onClick={() => navigate("support")}>
                  账户支持
                </button>
              )}
              <button className="patient-secondary" onClick={logout}>
                退出登录
              </button>
            </nav>
            {view === "identity" && (
              <section>
                <h2>我的身份</h2>
                <dl>
                  <dt>本系统账户</dt>
                  <dd>{me.user_id}</dd>
                  <dt>所属机构</dt>
                  <dd>{me.tenant_id}</dd>
                  <dt>系统角色</dt>
                  <dd>
                    {me.roles
                      .map((role) => roles[role] ?? "未开放角色")
                      .join("、")}
                  </dd>
                </dl>
                <p>系统角色声明不等于医院身份核验。</p>
                {me.roles.some((role) =>
                  [
                    "org_admin",
                    "support",
                    "platform_operator",
                    "knowledge_admin",
                    "integration_admin",
                    "auditor",
                  ].includes(role),
                ) && (
                  <p>
                    管理身份不授予患者内容访问权；仅开放当前服务器明确授权的功能，账户支持不包含身份管理。
                  </p>
                )}
                {(!me.binding.verified || !me.permissions.health_consent) && (
                  <p>
                    需机构完成身份与绑定核验，并确认年龄及监护规则；未核验、未成年人、监护人及年龄未知路径不开放患者内容。
                  </p>
                )}
              </section>
            )}
            {view === "audit" && (
              <AuditTable
                rows={audit}
                offset={offset}
                busy={busy}
                page={pageAudit}
              />
            )}
            {view === "support" && (
              <SupportCases
                queue={me.permissions.support_queue}
                account={me.user_id}
                busy={busy}
                read={supportRead}
                mutate={mutate}
              />
            )}
            {view === "consent" && (
              <section>
                <h2>健康信息使用授权</h2>
                <p>
                  不包含医院病历接入或外部模型处理。撤回仅停止未来目的使用，不删除历史记录。
                </p>
                {!notice ? (
                  <p role="status">正在读取机构告知…</p>
                ) : (
                  <>
                    <h3>机构告知 · {notice.notice.version}</h3>
                    <p className="patient-prose">{notice.notice.text}</p>
                    <p role="status">
                      {notice.granted ? "当前已授权" : "当前未授权"}
                      {notice.expires_at
                        ? ` · 有效至 ${notice.expires_at}`
                        : ""}
                    </p>
                    {notice.granted ? (
                      <button disabled={busy} onClick={withdraw}>
                        撤回授权
                      </button>
                    ) : (
                      <>
                        <label className="patient-check">
                          <input
                            type="checkbox"
                            checked={accepted}
                            onChange={(event) =>
                              setAccepted(event.target.checked)
                            }
                          />
                          我已完整阅读上述机构告知并明确同意本目的使用
                        </label>
                        <button
                          disabled={!accepted || busy}
                          onClick={() =>
                            mutate(async (token, live) => {
                              const updated = consent(
                                await request(
                                  "/me/consents/health",
                                  "POST",
                                  {
                                    version: notice.notice.version,
                                    accepted: true,
                                  },
                                  token,
                                ),
                              );
                              if (!live()) return;
                              const own = identity(await request("/me"));
                              if (live()) {
                                setNotice(updated);
                                setAccepted(false);
                                setMe(own);
                              }
                            })
                          }
                        >
                          明确同意
                        </button>
                      </>
                    )}
                  </>
                )}
              </section>
            )}
            {view === "conversations" && (
              <section>
                <h2>会话记录</h2>
                <p>仅保存您输入的文字记录，不生成医疗建议或助手回复。</p>
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    mutate(async (token, live) => {
                      row(
                        await request(
                          "/conversations",
                          "POST",
                          { title },
                          token,
                        ),
                        "title",
                      );
                      if (!live()) return;
                      const page = rows(
                        await request("/conversations?limit=20&offset=0"),
                        "title",
                      );
                      if (live()) {
                        setTitle("");
                        setList(page);
                        setOffset(0);
                      }
                    });
                  }}
                >
                  <label>
                    会话标题
                    <input
                      maxLength={200}
                      value={title}
                      onChange={(event) => setTitle(event.target.value)}
                    />
                  </label>
                  <button disabled={busy || !title.trim()}>创建会话</button>
                </form>
                <ul>
                  {list.map((item) => (
                    <li key={item.id}>
                      <button
                        disabled={busy}
                        onClick={() => {
                          generation.current++;
                          setSelected(item.id);
                          setTurns([]);
                          setContent("");
                          setTurnOffset(0);
                          void perform(async (live) => {
                            const data = rows(
                              await request(
                                `/conversations/${item.id}/turns?limit=20&offset=0`,
                              ),
                              "content",
                            );
                            if (live()) setTurns(data);
                          });
                        }}
                      >
                        {item.title}
                      </button>
                    </li>
                  ))}
                </ul>
                <div className="patient-paging">
                  <button
                    disabled={busy || offset === 0}
                    onClick={() => page(-20)}
                  >
                    上一页会话
                  </button>
                  <span>第 {offset / 20 + 1} 页</span>
                  <button
                    disabled={busy || list.length === 0 || offset >= 10000}
                    onClick={() => page(20)}
                  >
                    下一页会话
                  </button>
                </div>
                {selected && (
                  <section>
                    <h3>文字记录</h3>
                    <ol>
                      {turns.map((item) => (
                        <li className="patient-prose" key={item.id}>
                          {item.content}
                        </li>
                      ))}
                    </ol>
                    <div className="patient-paging">
                      <button
                        disabled={busy || turnOffset === 0}
                        onClick={() => page(-20, true)}
                      >
                        上一页记录
                      </button>
                      <button
                        disabled={
                          busy || turns.length === 0 || turnOffset >= 10000
                        }
                        onClick={() => page(20, true)}
                      >
                        下一页记录
                      </button>
                    </div>
                    <form
                      onSubmit={(event) => {
                        event.preventDefault();
                        mutate(async (token, live) => {
                          row(
                            await request(
                              `/conversations/${selected}/turns`,
                              "POST",
                              { content },
                              token,
                            ),
                            "content",
                          );
                          if (!live()) return;
                          const page = rows(
                            await request(
                              `/conversations/${selected}/turns?limit=20&offset=0`,
                            ),
                            "content",
                          );
                          if (live()) {
                            setContent("");
                            setTurns(page);
                            setTurnOffset(0);
                          }
                        });
                      }}
                    >
                      <label>
                        记录内容
                        <textarea
                          maxLength={16000}
                          value={content}
                          onChange={(event) => setContent(event.target.value)}
                        />
                      </label>
                      <button disabled={busy || !content.trim()}>
                        保存记录
                      </button>
                    </form>
                  </section>
                )}
              </section>
            )}
          </>
        )}
      </main>
      <footer>
        机构规则与真实身份核验仍须由可信机构流程完成。本环境不代表生产、临床或法律验收。
      </footer>
    </div>
  );
}
