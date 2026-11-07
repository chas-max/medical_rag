import { useCallback, useEffect, useRef, useState } from "react";
import { object, PatientError, request } from "./client";
import { auditRows, type AuditRow } from "./audit";
import { AccountMenu } from "./AccountMenu";
import { AuditTable } from "./AuditTable";
import { SupportCases } from "./SupportCases";
import {
  identity,
  chatResult,
  consent,
  row,
  rows,
  roles,
  type Citation,
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

/** No hash means the product landing page: the question/record interface. */
function hashTarget(): { target: View; fromHash: boolean } {
  switch (window.location.hash) {
    case "#consent":
      return { target: "consent", fromHash: true };
    case "#audit":
      return { target: "audit", fromHash: true };
    case "#conversations":
      return { target: "conversations", fromHash: true };
    case "#support":
      return { target: "support", fromHash: true };
    case "#identity":
      return { target: "identity", fromHash: true };
    default:
      return { target: "conversations", fromHash: false };
  }
}

function deriveTitle(text: string) {
  return text.trim().replace(/\s+/g, " ").slice(0, 30) || "新对话";
}

function stamp(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.replace("T", " ").slice(0, 16);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Replace raw block ids with readable source numbers for display only. */
function answerText(value: string, sources: Citation[]) {
  return sources.reduce(
    (text, source, index) =>
      text.split(`[${source.chunk_id}]`).join(`[${index + 1}]`),
    value,
  );
}

function sourceLabel(source: Citation) {
  return [
    source.filename,
    source.section,
    source.page !== undefined ? `第 ${source.page} 页` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

export function PatientApp() {
  const [status, setStatus] = useState("loading"),
    [me, setMe] = useState<Identity | null>(null),
    [view, setView] = useState<View>("conversations");
  const [notice, setNotice] = useState<Consent | null>(null),
    [accepted, setAccepted] = useState(false),
    [list, setList] = useState<Row[]>([]),
    [turns, setTurns] = useState<Row[]>([]);
  const [audit, setAudit] = useState<AuditRow[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null),
    [content, setContent] = useState(""),
    [pendingQuestion, setPendingQuestion] = useState<string | null>(null),
    [chatError, setChatError] = useState(""),
    [offset, setOffset] = useState(0),
    [turnOffset, setTurnOffset] = useState(0),
    [busy, setBusy] = useState(false),
    [menu, setMenu] = useState(false);
  const generation = useRef(0),
    csrf = useRef<string | null>(null),
    pending = useRef(false),
    composer = useRef<HTMLTextAreaElement>(null);
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
    setContent("");
    setPendingQuestion(null);
    setChatError("");
    setOffset(0);
    setTurnOffset(0);
    setBusy(false);
    setMenu(false);
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
    (target: View = "conversations", fromHash = false, force = false) => {
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
        if (!force && barrier.status !== "withdrawn") {
          setStatus(barrier.status);
          return;
        }
        if (!force) target = "consent";
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
          // The hash explicitly asked for a view this account may not use.
          if (fromHash) {
            clear();
            setStatus("forbidden");
          }
          // A default landing without conversation access stays on the
          // question surface and shows the consent/authority gate instead of
          // pretending the content exists.
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
    const initial = hashTarget();
    refresh(initial.target, initial.fromHash);
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
    const hash = () => {
      const next = hashTarget();
      refresh(next.target, next.fromHash);
    };
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
    setMenu(false);
    generation.current++;
    setView(next);
    setList([]);
    setAudit(null);
    setTurns([]);
    setSelected(null);
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
  const fresh = () => {
    if (pending.current || terminal.current || !active.current) return;
    if (!me?.permissions.conversations) return;
    generation.current++;
    setSelected(null);
    setTurns([]);
    setContent("");
    setTurnOffset(0);
    setMenu(false);
    composer.current?.focus();
  };
  const select = (id: string) => {
    if (pending.current || terminal.current || !active.current) return;
    generation.current++;
    setSelected(id);
    setTurns([]);
    setContent("");
    setTurnOffset(0);
    setMenu(false);
    void perform(async (live) => {
      const data = rows(
        await request(`/conversations/${id}/turns?limit=20&offset=0`),
        "content",
      );
      if (live()) setTurns(data);
    });
  };
  // Records-only fallback: used when the operator has not configured an
  // answer service. It never fabricates an assistant reply.
  const record = (text: string) => {
    const selectedId = selected;
    mutate(async (token, live) => {
      let id = selectedId;
      if (!id) {
        const created = row(
          await request(
            "/conversations",
            "POST",
            { title: deriveTitle(text) },
            token,
          ),
          "title",
        );
        id = created.id;
        if (!live()) return;
        setSelected(id);
      }
      await request(
        `/conversations/${id}/turns`,
        "POST",
        { content: text },
        token,
      );
      if (!live()) return;
      setContent("");
      const [page, pageTurns] = await Promise.all([
        request("/conversations?limit=20&offset=0"),
        request(`/conversations/${id}/turns?limit=20&offset=0`),
      ]);
      if (!live()) return;
      setList(rows(page, "title"));
      setTurns(rows(pageTurns, "content"));
      setOffset(0);
      setTurnOffset(0);
    });
  };
  const ask = () => {
    const text = content.trim();
    if (!text || !me?.permissions.conversations) return;
    if (pending.current || !csrf.current || terminal.current || !active.current)
      return;
    if (me.permissions.chat !== true) {
      record(text);
      return;
    }
    const selectedId = selected;
    setPendingQuestion(text);
    setChatError("");
    mutate(async (token, live) => {
      try {
        const result = chatResult(
          await request(
            "/chat",
            "POST",
            { conversation_id: selectedId, question: text },
            token,
          ),
        );
        if (!live()) return;
        const id = result.conversation.id;
        setSelected(id);
        setContent("");
        setPendingQuestion(null);
        const [page, pageTurns] = await Promise.all([
          request("/conversations?limit=20&offset=0"),
          request(`/conversations/${id}/turns?limit=20&offset=0`),
        ]);
        if (!live()) return;
        setList(rows(page, "title"));
        setTurns(rows(pageTurns, "content"));
        setOffset(0);
        setTurnOffset(0);
      } catch (error) {
        if (!live()) return;
        setPendingQuestion(null);
        // A model/retrieval outage is a recoverable inline error, not a
        // session failure: keep the conversation and let the user retry.
        if (
          error instanceof PatientError &&
          error.status !== 401 &&
          error.status !== 403
        ) {
          // 409 means another write changed the conversation while the answer was
          // generated: keep the input and the current conversation, never retry
          // silently with a stale snapshot.
          setChatError(
            error.status === 409
              ? "会话已更新，请重新发送问题。"
              : "问答服务暂不可用，请稍后重试。",
          );
          return;
        }
        throw error;
      }
    });
  };
  const current = selected ? list.find((item) => item.id === selected) : null;
  const canChat = me?.permissions.conversations === true;
  const hasAnswers = me?.permissions.chat === true;
  return (
    <div className="patient-shell">
      {status !== "ready" || !me ? (
        <main className="patient-status">
          <div className="patient-status-brand">
            <span aria-hidden="true">✳</span> 知源 · 患者门户
          </div>
          {status === "loading" && <p role="status">正在安全验证…</p>}
          {status === "hidden" && (
            <p role="status">页面已锁定，返回后重新验证。</p>
          )}
          {status === "login" && (
            <section>
              <h1>机构身份验证</h1>
              <p>登录后进入问答与记录界面。</p>
              <form action="/auth/login" method="post">
                <button type="submit">机构登录</button>
              </form>
            </section>
          )}
          {status === "unavailable" && (
            <section>
              <p role="alert">服务暂不可用，请重试。</p>
              <button onClick={() => refresh("conversations", false, true)}>
                重试
              </button>
            </section>
          )}
          {status === "forbidden" && (
            <section>
              <p role="alert">403 · 当前权限不允许访问此内容。</p>
              <button onClick={() => refresh("conversations", false, true)}>
                重新验证身份
              </button>
            </section>
          )}
        </main>
      ) : (
        <>
          <aside className="patient-sidebar">
            <div className="patient-brand">
              <span className="patient-brand-icon" aria-hidden="true">
                ✳
              </span>
              <span>
                <strong>知源</strong>
                <small>患者门户 · 虚构开发</small>
              </span>
            </div>
            <button
              type="button"
              className="patient-new"
              disabled={busy || !canChat}
              onClick={fresh}
            >
              ＋ 新对话
            </button>
            <div className="patient-threads">
              <p className="patient-side-label">最近会话</p>
              {canChat ? (
                list.length ? (
                  <ul>
                    {list.map((item) => (
                      <li key={item.id}>
                        <button
                          type="button"
                          className="patient-thread-link"
                          aria-current={selected === item.id}
                          disabled={busy}
                          onClick={() => select(item.id)}
                        >
                          {item.title}
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="patient-side-empty">暂无会话</p>
                )
              ) : (
                <p className="patient-side-empty">
                  {me.permissions.health_consent
                    ? "完成健康信息授权后可保存会话。"
                    : "当前账号不开放患者会话。"}
                </p>
              )}
              {canChat && (offset > 0 || list.length > 0) && (
                <div className="patient-paging patient-paging-side">
                  <button
                    type="button"
                    className="patient-ghost"
                    disabled={busy || offset === 0}
                    onClick={() => page(-20)}
                  >
                    上一页会话
                  </button>
                  <button
                    type="button"
                    className="patient-ghost"
                    disabled={busy || list.length === 0 || offset >= 10000}
                    onClick={() => page(20)}
                  >
                    下一页会话
                  </button>
                </div>
              )}
            </div>
            <nav className="patient-nav" aria-label="服务导航">
              {canChat && (
                <button
                  type="button"
                  className="patient-nav-link"
                  aria-current={view === "conversations"}
                  disabled={busy}
                  onClick={() => navigate("conversations")}
                >
                  会话记录
                </button>
              )}
              <button
                type="button"
                className="patient-nav-link"
                aria-current={view === "identity"}
                disabled={busy}
                onClick={() => navigate("identity")}
              >
                我的身份
              </button>
              {me.permissions.health_consent && (
                <button
                  type="button"
                  className="patient-nav-link"
                  aria-current={view === "consent"}
                  disabled={busy}
                  onClick={() => navigate("consent")}
                >
                  健康信息授权
                </button>
              )}
              {me.permissions.audit && (
                <button
                  type="button"
                  className="patient-nav-link"
                  aria-current={view === "audit"}
                  disabled={busy}
                  onClick={() => navigate("audit")}
                >
                  安全审计
                </button>
              )}
              {me.permissions.support_self && (
                <button
                  type="button"
                  className="patient-nav-link"
                  aria-current={view === "support"}
                  disabled={busy}
                  onClick={() => navigate("support")}
                >
                  账户支持
                </button>
              )}
            </nav>
            <p className="patient-side-note">
              虚构数据开发环境 · 回答不替代医生诊断
            </p>
            <AccountMenu
              me={me}
              open={menu}
              disabled={busy}
              onToggle={() => setMenu((value) => !value)}
              onClose={() => setMenu(false)}
              onLogout={logout}
            />
          </aside>
          <main className="patient-main">
            {view === "conversations" && canChat && (
              <div className="patient-thread">
                <header className="patient-thread-head">
                  <div>
                    <h1>{current?.title ?? "医疗问答"}</h1>
                    <p>
                      {hasAnswers
                        ? "基于公开医疗资料检索并生成带引用的回答，不替代医生诊断；请勿输入患者隐私。"
                        : "仅保存你输入的文字记录；当前未配置问答服务。"}
                    </p>
                  </div>
                </header>
                <div className="patient-messages" role="log" aria-label="会话消息">
                  {turns.length === 0 && !pendingQuestion ? (
                    <div className="patient-welcome">
                      <h2>
                        {selected ? "这条会话还没有记录" : "开始新的对话"}
                      </h2>
                      <p>
                        {hasAnswers
                          ? "输入健康或医疗资料相关的问题，系统会检索公开资料并给出带来源的回答；资料不足时会明确拒答。"
                          : "在下方输入想保存的内容。发送后会创建会话并保存为你的文字记录。"}
                      </p>
                      <p className="patient-note">
                        {hasAnswers
                          ? "回答由机构配置的模型基于公开资料生成，仅供参考，不替代医生诊断。"
                          : "当前未配置问答服务，发送只会保存文字记录。"}
                      </p>
                    </div>
                  ) : (
                    <ol className="patient-turn-list">
                      {turns.map((item) => (
                        <li
                          className={`patient-turn patient-turn-${item.role}`}
                          key={item.id}
                        >
                          <div className="patient-bubble">
                            <p>
                              {item.role === "assistant"
                                ? answerText(item.content ?? "", item.citations)
                                : item.content}
                            </p>
                            {item.role === "assistant" &&
                              item.citations.length > 0 && (
                                <ol className="patient-sources" aria-label="引用来源">
                                  {item.citations.map((source, index) => (
                                    <li key={`${source.chunk_id}-${index}`}>
                                      [{index + 1}] {sourceLabel(source)}
                                    </li>
                                  ))}
                                </ol>
                              )}
                          </div>
                          <time dateTime={item.created_at}>
                            {stamp(item.created_at)}
                          </time>
                        </li>
                      ))}
                      {pendingQuestion && (
                        <li className="patient-turn patient-turn-user">
                          <div className="patient-bubble">
                            <p>{pendingQuestion}</p>
                          </div>
                        </li>
                      )}
                      {pendingQuestion && (
                        <li className="patient-turn patient-turn-assistant">
                          <div
                            className="patient-bubble patient-thinking"
                            role="status"
                          >
                            正在检索公开资料并生成回答…
                          </div>
                        </li>
                      )}
                    </ol>
                  )}
                </div>
                {selected && (
                  <div className="patient-paging">
                    <button
                      type="button"
                      className="patient-ghost"
                      disabled={busy || turnOffset === 0}
                      onClick={() => page(-20, true)}
                    >
                      上一页记录
                    </button>
                    <button
                      type="button"
                      className="patient-ghost"
                      disabled={
                        busy || turns.length === 0 || turnOffset >= 10000
                      }
                      onClick={() => page(20, true)}
                    >
                      下一页记录
                    </button>
                  </div>
                )}
                {chatError && (
                  <p role="alert" className="patient-chat-error">
                    {chatError}
                  </p>
                )}
                <form
                  className="patient-composer"
                  onSubmit={(event) => {
                    event.preventDefault();
                    ask();
                  }}
                >
                  <label className="patient-sr" htmlFor="patient-message">
                    输入消息
                  </label>
                  <textarea
                    id="patient-message"
                    ref={composer}
                    rows={3}
                    maxLength={16000}
                    placeholder={
                      hasAnswers
                        ? "输入你的问题，例如：高血压患者日常如何监测血压？"
                        : "输入要保存的文字记录…"
                    }
                    value={content}
                    onChange={(event) => setContent(event.target.value)}
                  />
                  <div className="patient-composer-bar">
                    <span className="patient-note">
                      {hasAnswers
                        ? "回答基于公开医疗资料，不替代医生诊断。"
                        : "发送即保存文字记录；当前未配置问答服务。"}
                    </span>
                    <button
                      type="submit"
                      disabled={busy || !content.trim()}
                    >
                      发送
                    </button>
                  </div>
                </form>
              </div>
            )}
            {view === "conversations" && !canChat && (
              <div className="patient-thread patient-gate">
                <h1>
                  {me.permissions.health_consent
                    ? "需要健康信息授权"
                    : "当前账号不开放患者会话"}
                </h1>
                <p>
                  {me.permissions.health_consent
                    ? hasAnswers
                      ? "授权后可以向公开医疗资料问答，并在你自己的会话中保存问答记录。"
                      : "授权后可以创建会话并保存你自己的文字记录。"
                    : "该入口只对已核验并授权的患者开放；管理身份不会因此获得患者内容访问权。"}
                </p>
                {me.permissions.health_consent ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => navigate("consent")}
                  >
                    前往健康信息授权
                  </button>
                ) : (
                  !me.roles.some((role) =>
                    [
                      "org_admin",
                      "support",
                      "platform_operator",
                      "knowledge_admin",
                      "integration_admin",
                      "auditor",
                    ].includes(role),
                  ) && (
                    <p role="status">
                      需机构完成身份与绑定核验，并确认年龄及监护规则。
                    </p>
                  )
                )}
              </div>
            )}
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
          </main>
        </>
      )}
    </div>
  );
}
