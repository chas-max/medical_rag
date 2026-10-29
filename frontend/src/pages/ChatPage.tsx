import { useEffect, useRef, useState } from "react";
import AnswerContent from "../components/AnswerContent";
import RunTrace, { RunProgress } from "../components/RunTrace";
import { safeProgress, safeRunTrace } from "../workflow/runTrace";
import {
  streamChat,
  confirmProfile,
  type ChatPayload,
  type ConfirmationPayload,
} from "../api/chat";
import { ProfileNotice } from "../chat/ProfileNotice";
import { EpisodeNotice } from "../chat/EpisodeNotice";
import { DocumentJobList } from "../chat/DocumentJobList";
import { useDocumentUploads } from "../chat/useDocumentUploads";
import {
  getConversations,
  getConversation,
  importConversations,
  type Conversation,
  type Message,
} from "../api/conversations";
import {
  getHealth,
  type AnalysisPayload,
  type Citation,
  type ClaimPayload,
  type StatusPayload,
  type NodeProgressPayload,
} from "../api/workflows";
import { getChunk, type ChunkDetail } from "../api/sources";
const STORAGE = "zhiyuan-conversations-v1";

function parseConversations(saved: unknown): Conversation[] {
  try {
    if (!Array.isArray(saved)) return [];
    return saved
      .filter(
        (item) =>
          item &&
          typeof item.id === "string" &&
          typeof item.title === "string" &&
          Array.isArray(item.messages),
      )
      .map((item) => ({
        id: item.id,
        title: item.title,
        messages: item.messages
          .filter(
            (message: Message) =>
              message &&
              ["user", "assistant"].includes(message.role) &&
              typeof message.content === "string",
          )
          .map((message: Message) => ({
            ...message,
            pending: undefined,
            trace: safeRunTrace(message.trace),
          })),
        failedRuns: (Array.isArray(item.failedRuns)
          ? item.failedRuns
          : item.failedRun
            ? [item.failedRun]
            : []
        )
          .filter(
            (run: { runId?: unknown } | null) =>
              run && typeof run.runId === "string",
          )
          .map((run: { runId: string; trace?: unknown }) => ({
            runId: run.runId,
            trace: safeRunTrace(run.trace),
          })),
      }));
  } catch {
    return [];
  }
}

function initialConversations(): Conversation[] {
  try {
    return parseConversations(
      JSON.parse(localStorage.getItem(STORAGE) || "[]"),
    );
  } catch {
    return [];
  }
}

/** 刷新中断的流式回答按普通消息保存，不留下永远"生成中"的占位。 */
function persisted(conversations: Conversation[]): Conversation[] {
  return conversations.map((item) => ({
    ...item,
    messages: item.messages.map((message) => ({
      ...message,
      pending: undefined,
    })),
  }));
}

// 拒答徽标必须与回答正文一致：解析失败/截断是生成侧故障，不能再显示成“证据不足”，
// 否则用户看到的正文写着“格式异常”，旁边的标签却把责任推给资料。
const ABSTAIN_LABELS: Record<string, string> = {
  ABSTAIN_PARSE_ERROR: "生成异常",
  ABSTAIN_TRUNCATED: "输出截断",
  ABSTAIN_UNANCHORED: "引用校验未通过",
  ABSTAIN_DEGRADED: "检索降级",
};

function abstainLabel(reason?: string): string {
  return (reason && ABSTAIN_LABELS[reason]) || "证据不足";
}

export default function ChatPage() {
  const uploads = useDocumentUploads();
  const [conversations, setConversations] =
    useState<Conversation[]>(initialConversations);
  const initialCache = useRef(conversations);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyNotice, setHistoryNotice] = useState("");
  const inFlight = useRef<string | null>(null);
  const revision = useRef(0);
  const [current, setCurrent] = useState<string | null>(null);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const currentRef = useRef<string | null>(null);
  // 默认 8 条证据：5 条时模型可引用的块太少，回答容易只剩两三条结论、显得干瘪。
  const [topK, setTopK] = useState(8);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [question, setQuestion] = useState("");
  const [retries, setRetries] = useState<Record<string, ChatPayload>>({});
  const confirmations = useRef<Record<string, ConfirmationPayload>>({});
  const [chatStages, setChatStages] = useState<
    Record<string, "intent" | "profile" | "answer">
  >({});
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [failureNotices, setFailureNotices] = useState<Record<string, string>>(
    {},
  );
  const [stages, setStages] = useState<Record<string, StatusPayload>>({});
  const [agentStages, setAgentStages] = useState<
    Record<string, NodeProgressPayload>
  >({});
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(
    null,
  );
  const [chunkDetail, setChunkDetail] = useState<ChunkDetail | null>(null);
  const [chunkLoading, setChunkLoading] = useState(false);
  const chunkKey = selectedCitation?.chunk_id;
  useEffect(() => {
    if (!chunkKey) {
      setChunkDetail(null);
      setChunkLoading(false);
      return;
    }
    let cancelled = false;
    setChunkDetail(null);
    setChunkLoading(true);
    getChunk(chunkKey)
      .then((detail) => {
        if (!cancelled) setChunkDetail(detail);
      })
      .catch(() => {
        if (!cancelled) setChunkDetail(null);
      })
      .finally(() => {
        if (!cancelled) setChunkLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [chunkKey]);
  const fileRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const active = conversations.find((item) => item.id === current);
  const citationSourceRaw =
    typeof selectedCitation?.section === "string" && selectedCitation.section
      ? selectedCitation.section
      : typeof selectedCitation?.source === "string"
        ? selectedCitation.source
        : "";
  const citationUrl = /^https?:\/\//i.test(citationSourceRaw)
    ? citationSourceRaw
    : "";
  const failureNotice = current ? failureNotices[current] : undefined;
  const streaming = !!active?.messages.at(-1)?.pending;
  const stage = current ? stages[current] : undefined;
  const chatStage = current ? chatStages[current] : undefined;
  const waitingText =
    chatStage === "intent"
      ? "正在分析本次意图…"
      : chatStage === "profile"
        ? "正在处理个人画像…"
        : streaming
          ? "正在生成并校验后续引用…"
          : stage
            ? `已检索到 ${stage.evidence} 条证据，正在生成回答${stage.web_search ? "（必要时联网补充）" : ""}…`
            : "正在检索并验证证据…";

  function clearFailure(id: string) {
    setFailureNotices((previous) => {
      if (!(id in previous)) return previous;
      const next = { ...previous };
      delete next[id];
      return next;
    });
  }

  function setStage(id: string, status: StatusPayload) {
    setStages((previous) => ({ ...previous, [id]: status }));
  }

  function clearStage(id: string) {
    setChatStages((previous) => {
      const next = { ...previous };
      delete next[id];
      return next;
    });
    setAgentStages((previous) => {
      const next = { ...previous };
      delete next[id];
      return next;
    });
    setStages((previous) => {
      if (!(id in previous)) return previous;
      const next = { ...previous };
      delete next[id];
      return next;
    });
  }

  /** 把一个已通过引用校验的结论追加到流式回答上。 */
  function appendClaim(conversationId: string, claim: ClaimPayload) {
    setConversations((previous) =>
      previous.map((item) => {
        if (item.id !== conversationId) return item;
        const messages = [...item.messages];
        const last = messages[messages.length - 1];
        if (last?.role === "assistant" && last.pending) {
          const citations = [...(last.citations || [])];
          const known = new Set(citations.map((citation) => citation.chunk_id));
          for (const citation of claim.citations) {
            if (citation.chunk_id && known.has(citation.chunk_id)) continue;
            known.add(citation.chunk_id);
            citations.push(citation);
          }
          messages[messages.length - 1] = {
            ...last,
            content: last.content
              ? `${last.content}\n${claim.text}`
              : claim.text,
            citations,
          };
        } else {
          messages.push({
            role: "assistant",
            content: claim.text,
            citations: claim.citations,
            pending: true,
          });
        }
        return { ...item, messages };
      }),
    );
  }

  /** 解释段固定排在所有结论之后，只落在最后一条待定回答上。 */
  function appendAnalysis(conversationId: string, analysis: AnalysisPayload) {
    setConversations((previous) =>
      previous.map((item) => {
        if (item.id !== conversationId) return item;
        const messages = [...item.messages];
        const last = messages[messages.length - 1];
        if (last?.role === "assistant" && last.pending) {
          messages[messages.length - 1] = { ...last, analysis };
        }
        return { ...item, messages };
      }),
    );
  }

  /** 失败或空回答时不留下半截流式内容，行为与整段生成时一致。 */
  function dropPendingAnswer(conversationId: string) {
    setConversations((previous) =>
      previous.map((item) => {
        if (item.id !== conversationId) return item;
        const last = item.messages[item.messages.length - 1];
        if (last?.role !== "assistant" || !last.pending) return item;
        return { ...item, messages: item.messages.slice(0, -1) };
      }),
    );
  }

  /** 用最终校验结果替换流式占位，未流式时直接追加。 */
  function settleAnswer(conversationId: string, message: Message) {
    setConversations((previous) =>
      previous.map((item) => {
        if (item.id !== conversationId) return item;
        const last = item.messages[item.messages.length - 1];
        const existing = message.runId
          ? item.messages.findIndex(
              (entry) =>
                entry.role === "assistant" && entry.runId === message.runId,
            )
          : -1;
        return {
          ...item,
          messages:
            existing >= 0
              ? item.messages.map((entry, index) =>
                  index === existing ? message : entry,
                )
              : last?.role === "assistant" && last.pending
                ? [...item.messages.slice(0, -1), message]
                : [...item.messages, message],
        };
      }),
    );
  }

  useEffect(() => {
    getHealth()
      .then((result) =>
        setHealthy(result.milvus && result.reranker_available !== false),
      )
      .catch(() => setHealthy(false));
  }, []);
  useEffect(() => {
    let mounted = true;
    async function loadHistory() {
      try {
        let saved = await getConversations();
        const known = new Set(saved.map((item) => item.id));
        const legacy = initialCache.current.filter(
          (item) => !known.has(item.id),
        );
        if (legacy.length) saved = await importConversations(persisted(legacy));
        if (mounted) {
          setConversations(parseConversations(saved));
          setHistoryLoading(false);
        }
      } catch {
        if (mounted)
          setHistoryNotice(
            "历史记录同步失败，暂时显示浏览器缓存；为保护旧记录已暂停发送，请检查后端连接后刷新。",
          );
      }
    }
    void loadHistory();
    return () => {
      mounted = false;
    };
  }, []);
  useEffect(() => {
    if (!current || historyLoading || inFlight.current === current) return;
    let mounted = true;
    const requestedRevision = revision.current;
    getConversation(current)
      .then((saved) => {
        if (
          !mounted ||
          revision.current !== requestedRevision ||
          inFlight.current === current
        )
          return;
        const loaded = parseConversations([saved])[0];
        if (loaded)
          setConversations((previous) =>
            previous.map((item) => (item.id === current ? loaded : item)),
          );
      })
      .catch(() => {
        if (mounted)
          setHistoryNotice(
            "无法刷新该对话，暂时显示已缓存的记录；请检查后端连接后重新打开。",
          );
      });
    return () => {
      mounted = false;
    };
  }, [current, historyLoading, historyRefresh]);
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE, JSON.stringify(persisted(conversations)));
    } catch {
      setHistoryNotice("浏览器历史缓存不可用；已完成的回答仍由服务端保存。");
    }
  }, [conversations]);
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [active?.messages.length, active?.messages.at(-1)?.content.length]);
  async function send(value = question, retry?: ChatPayload) {
    const text = value.trim();
    if ((!text && !retry) || busy || inFlight.current || historyLoading) return;
    const id =
      retry?.conversation_id || currentRef.current || crypto.randomUUID();
    const payload: ChatPayload = retry || {
      request_id: crypto.randomUUID(),
      conversation_id: id,
      workflow_id: "medical-answer",
      inputs: { question: text, provider: "qwen", top_k: topK },
    };
    inFlight.current = id;
    revision.current += 1;
    clearFailure(id);
    clearStage(id);
    currentRef.current = id;
    setCurrent(id);
    if (!retry)
      setConversations((previous) => {
        const exists = previous.some((item) => item.id === id);
        return exists
          ? previous.map((item) =>
              item.id === id
                ? {
                    ...item,
                    messages: [
                      ...item.messages,
                      { role: "user", content: text },
                    ],
                  }
                : item,
            )
          : [
              {
                id,
                title: text.slice(0, 24),
                messages: [{ role: "user", content: text }],
              },
              ...previous,
            ];
      });
    setQuestion("");
    setNotice("");
    setBusy(true);
    setRetries((previous) => {
      const next = { ...previous };
      delete next[id];
      return next;
    });
    try {
      const result = await streamChat(payload, (event) => {
        if (event.event === "claim") appendClaim(id, event.data);
        else if (event.event === "analysis") appendAnalysis(id, event.data);
        else if (event.event === "chat.progress")
          setChatStages((previous) => ({
            ...previous,
            [id]: event.data.stage,
          }));
        else if (event.event === "status") {
          setChatStages((previous) => ({ ...previous, [id]: "answer" }));
          setStage(id, event.data);
        } else if (event.event === "node.progress") {
          const progress = safeProgress(event.data);
          if (progress)
            setAgentStages((previous) => ({ ...previous, [id]: progress }));
        }
      });
      if (result.status === "failed") {
        setConversations((previous) =>
          previous.map((item) =>
            item.id === id
              ? {
                  ...item,
                  failedRuns: [
                    ...(item.failedRuns || []),
                    {
                      runId: result.run_id,
                      trace: safeRunTrace(result.trace),
                    },
                  ],
                }
              : item,
          ),
        );
        dropPendingAnswer(id);
        if (result.profile || result.episode)
          settleAnswer(id, {
            role: "assistant",
            content: "",
            runId: result.run_id,
            kind: result.kind,
            profile: result.profile,
            episode: result.episode,
            memory: result.memory,
            error: result.error,
          });
        const failure = result.trace.find(
          (item) => item.status === "failed",
        )?.error;
        setFailureNotices((previous) => ({
          ...previous,
          [id]:
            result.error?.message ||
            failure?.message ||
            "回答失败，请检查服务后重试",
        }));
        return;
      }
      if (!result.output.abstained && !result.output.answer?.trim()) {
        setConversations((previous) =>
          previous.map((item) =>
            item.id === id
              ? {
                  ...item,
                  failedRuns: [
                    ...(item.failedRuns || []),
                    {
                      runId: result.run_id,
                      trace: safeRunTrace(result.trace),
                    },
                  ],
                }
              : item,
          ),
        );
        dropPendingAnswer(id);
        if (result.profile || result.episode)
          settleAnswer(id, {
            role: "assistant",
            content: "",
            runId: result.run_id,
            kind: result.kind,
            profile: result.profile,
            episode: result.episode,
            memory: result.memory,
          });
        setFailureNotices((previous) => ({
          ...previous,
          [id]: "该工作流没有产生回答",
        }));
        return;
      }
      // 最终结果以服务端权威输出为准，覆盖流式阶段累积的内容。
      settleAnswer(id, {
        role: "assistant",
        content: result.output.answer || "该工作流没有产生回答",
        citations: result.output.citations || [],
        analysis: result.output.analysis as AnalysisPayload | undefined,
        abstained: result.output.abstained,
        abstainReason: result.output.abstain_reason,
        runId: result.run_id,
        trace: safeRunTrace(result.trace),
        kind: result.kind,
        profile: result.profile,
        episode: result.episode,
        memory: result.memory,
      });
    } catch (error) {
      dropPendingAnswer(id);
      setRetries((previous) => ({ ...previous, [id]: payload }));
      setFailureNotices((previous) => ({
        ...previous,
        [id]: error instanceof Error ? error.message : "请求失败",
      }));
    } finally {
      inFlight.current = null;
      clearStage(id);
      setBusy(false);
    }
  }

  async function confirmMessage(
    conversationId: string,
    index: number,
    message: Message,
    accept: boolean,
  ) {
    const token = message.profile?.confirmation_token;
    if (!token) return;
    const key = `${conversationId}:${token}:${accept}`;
    const payload = (confirmations.current[key] ||= {
      request_id: crypto.randomUUID(),
      conversation_id: conversationId,
      token,
      accept,
    });
    const result = await confirmProfile(payload);
    revision.current += 1;
    setConversations((previous) =>
      previous.map((item) =>
        item.id !== conversationId
          ? item
          : {
              ...item,
              messages: item.messages.map((entry, entryIndex) =>
                entryIndex === index &&
                entry.profile?.confirmation_token === token
                  ? { ...entry, profile: result.profile }
                  : entry,
              ),
            },
      ),
    );
  }

  async function upload(file: File) {
    await uploads.upload(file);
    if (fileRef.current) fileRef.current.value = "";
  }

  return (
    <div className="chat-layout">
      <aside className="chat-sidebar">
        <button
          className="new-chat"
          onClick={() => {
            currentRef.current = null;
            setCurrent(null);
            setQuestion("");
            setNotice("");
          }}
        >
          ＋ <span>新建对话</span>
          <span className="shortcut">NEW</span>
        </button>
        <div className="sidebar-heading">最近对话</div>
        <div className="conversation-list">
          {conversations.map((item) => (
            <button
              key={item.id}
              className={`conversation ${current === item.id ? "selected" : ""}`}
              onClick={() => {
                currentRef.current = item.id;
                setCurrent(item.id);
                setHistoryRefresh((value) => value + 1);
              }}
            >
              ☰ <span>{item.title}</span>
            </button>
          ))}
        </div>
        <div className="sidebar-bottom">
          <div className="sidebar-foot">✳ &nbsp; 知源工作台 · v0.1</div>
        </div>
      </aside>
      <main className="chat-main">
        <div className="chat-toolbar">
          <div>
            <span className="eyebrow">MEDICAL KNOWLEDGE ASSISTANT</span>
            <h1>
              医疗资料问答 <span className="toolbar-badge">BETA</span>
            </h1>
          </div>
          <span className={`health ${healthy ? "online" : ""}`}>
            <span className="status-dot" />
            {healthy === null
              ? "检查连接中"
              : healthy
                ? "知识库已连接"
                : "知识库未连接"}
          </span>
        </div>
        <div className="chat-content">
          {!active?.messages.length ? (
            <section className="welcome">
              <div className="welcome-mark">✳</div>
              <span className="eyebrow">ANSWER WITH EVIDENCE</span>
              <h2>从可信资料中寻找答案</h2>
              <p>
                可以提问公开医疗资料，也可以管理个人画像。医学回答会检索资料、核对证据，并展示引用。
              </p>
              <div className="suggestions">
                {[
                  "高血压患者日常管理有哪些要点？",
                  "糖尿病患者如何监测血糖？",
                  "有哪些常见的健康筛查建议？",
                ].map((item) => (
                  <button key={item} onClick={() => void send(item)}>
                    ↗ &nbsp;{item}
                  </button>
                ))}
              </div>
            </section>
          ) : (
            <div className="messages">
              {active.messages.map((message, index) => (
                <article className={`message ${message.role}`} key={index}>
                  <div className="avatar">
                    {message.role === "user" ? "你" : "✳"}
                  </div>
                  <div className="message-body">
                    <div className="message-author">
                      {message.role === "user" ? "你" : "知源助手"}
                      {message.abstained && (
                        <span className="abstain">
                          {abstainLabel(message.abstainReason)}
                        </span>
                      )}
                      {message.pending && (
                        <span className="streaming-badge">已校验 · 生成中</span>
                      )}
                    </div>
                    {message.role === "assistant" ? (
                      <AnswerContent
                        content={message.content}
                        citations={message.citations || []}
                        analysis={message.analysis}
                        abstained={!!message.abstained}
                        onCitationClick={setSelectedCitation}
                      />
                    ) : (
                      <div className="message-text">{message.content}</div>
                    )}
                    {message.role === "assistant" &&
                      !message.abstained &&
                      !!message.citations?.length && (
                        <div className="citations">
                          <span className="small-label">引用资料</span>
                          {message.citations.map((citation, index) => (
                            <button
                              key={index}
                              type="button"
                              onClick={() => setSelectedCitation(citation)}
                              aria-label={`来源 ${index + 1}：${citation.filename || citation.source || "资料"}`}
                            >
                              [{index + 1}]{" "}
                              {citation.filename || citation.source || "资料"}
                              {citation.page ? ` · 第 ${citation.page} 页` : ""}
                            </button>
                          ))}
                        </div>
                      )}
                    {message.role === "assistant" && (
                      <RunTrace trace={message.trace} runId={message.runId} />
                    )}
                    {message.profile && (
                      <ProfileNotice
                        key={`profile:${active.id}:${message.profile.confirmation_token || message.runId || index}`}
                        outcome={message.profile}
                        disabled={busy}
                        onConfirm={(accept) =>
                          confirmMessage(active.id, index, message, accept)
                        }
                      />
                    )}
                    {message.error && (
                      <p className="notice error" role="alert">
                        {message.error.message}
                      </p>
                    )}
                    {(message.episode || message.memory) && (
                      <EpisodeNotice
                        key={`episode:${active.id}:${message.runId || index}`}
                        runId={message.runId}
                        status={(message.episode || message.memory)!.status}
                        onStatus={(status) =>
                          setConversations((previous) =>
                            previous.map((item) =>
                              item.id !== active.id
                                ? item
                                : {
                                    ...item,
                                    messages: item.messages.map(
                                      (entry, entryIndex) =>
                                        entryIndex === index
                                          ? { ...entry, episode: { status } }
                                          : entry,
                                    ),
                                  },
                            ),
                          )
                        }
                      />
                    )}
                  </div>
                </article>
              ))}
              {active.failedRuns?.map((run, index) => (
                <RunTrace
                  key={`${run.runId}-${index}`}
                  trace={run.trace}
                  runId={run.runId}
                />
              ))}
              {current && agentStages[current] && (
                <RunProgress progress={agentStages[current]} />
              )}
              {busy && (
                <div className="thinking">
                  <span className="spinner" /> {waitingText}
                </div>
              )}
              <div ref={endRef} />
            </div>
          )}
        </div>
        <div className="composer-area">
          <div className="composer-wrap">
            <DocumentJobList jobs={uploads.jobs} />
            {(uploads.uploadNotice || uploads.connectionNotice) && (
              <div className="notice" role="status">
                {uploads.connectionNotice || uploads.uploadNotice}
              </div>
            )}
            {historyNotice && (
              <div className="notice" role="status">
                {historyNotice}
              </div>
            )}
            {notice && (
              <div className="notice" role="status">
                {notice}
                <button onClick={() => setNotice("")} aria-label="关闭提示">
                  ×
                </button>
              </div>
            )}
            {failureNotice && (
              <div className="notice error" role="alert">
                {failureNotice}
                {current && retries[current] && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => current && void send("", retries[current])}
                  >
                    重试此消息
                  </button>
                )}
                <button
                  onClick={() => current && clearFailure(current)}
                  aria-label="关闭错误提示"
                >
                  ×
                </button>
              </div>
            )}
            <details className="advanced-settings">
              <summary onClick={() => setAdvancedOpen((open) => !open)}>
                高级设置
              </summary>
              {advancedOpen && (
                <div className="settings-bar">
                  <label>
                    证据数{" "}
                    <select
                      aria-label="证据数"
                      value={topK}
                      onChange={(event) => setTopK(Number(event.target.value))}
                    >
                      {[3, 5, 8, 10].map((number) => (
                        <option key={number} value={number}>
                          {number}
                        </option>
                      ))}
                    </select>
                  </label>
                  <span className="settings-spacer" />
                </div>
              )}
            </details>
            <div className="composer">
              <textarea
                aria-label="输入医疗资料问题"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void send();
                  }
                }}
                placeholder="提问公开医疗资料，或管理个人画像…"
                rows={2}
              />
              <div className="composer-actions">
                <button
                  className="attach"
                  disabled={uploads.uploading}
                  onClick={() => fileRef.current?.click()}
                  title="上传公开资料"
                >
                  ♧ &nbsp;上传资料
                </button>
                <input
                  ref={fileRef}
                  hidden
                  type="file"
                  accept=".md,.txt,.pdf"
                  onChange={(event) => {
                    if (event.target.files?.[0])
                      void upload(event.target.files[0]);
                  }}
                />
                <span>支持 .md / .txt / .pdf</span>
                <button
                  className="send"
                  aria-label="发送问题"
                  disabled={busy || historyLoading || !question.trim()}
                  onClick={() => void send()}
                >
                  ↑
                </button>
              </div>
            </div>
            <p className="disclaimer">
              仅供公开资料检索参考，不替代医生诊断、治疗或紧急医疗建议。
            </p>
          </div>
        </div>
      </main>
      {selectedCitation && (
        <div
          className="citation-overlay"
          onClick={() => setSelectedCitation(null)}
        >
          <aside
            className="citation-panel"
            onClick={(event) => event.stopPropagation()}
          >
            <button className="close" onClick={() => setSelectedCitation(null)}>
              ×
            </button>
            <span className="eyebrow">SOURCE REFERENCE</span>
            <h2>引用原文</h2>
            <h3>
              {chunkDetail?.filename ||
                selectedCitation.filename ||
                selectedCitation.source ||
                "公开资料"}
            </h3>
            <p className="citation-meta">
              {(chunkDetail?.page ?? selectedCitation.page)
                ? `第 ${chunkDetail?.page ?? selectedCitation.page} 页 · `
                : ""}
              {citationUrl ? "联网资料" : String(selectedCitation.section || "原文摘录")}
            </p>
            {selectedCitation.excerpt && (
              <blockquote className="citation-excerpt">
                {selectedCitation.excerpt}
              </blockquote>
            )}
            {chunkLoading && (
              <p className="citation-status">正在载入资料原文…</p>
            )}
            {chunkDetail && (
              <>
                <span className="small-label">资料原文</span>
                <blockquote className="citation-fulltext">
                  {chunkDetail.parent_text || chunkDetail.text}
                </blockquote>
              </>
            )}
            {!chunkLoading && !chunkDetail && !selectedCitation.excerpt && (
              <p className="citation-status">
                {citationUrl
                  ? "该来源为联网检索网页，本地未留存原文，可打开原网页查看。"
                  : "未在资料库中找到该资料块。"}
              </p>
            )}
            {citationUrl && (
              <a
                className="citation-link"
                href={citationUrl}
                target="_blank"
                rel="noreferrer"
              >
                打开来源网页 ↗
              </a>
            )}
            <p className="disclaimer">请结合原始资料核查上下文。</p>
          </aside>
        </div>
      )}
    </div>
  );
}
