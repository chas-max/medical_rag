import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import AnswerContent from "../components/AnswerContent";
import {
  getHealth,
  getWorkflows,
  runWorkflow,
  uploadDocument,
  type Citation,
  type Workflow,
} from "../api/workflows";
import {
  readCurrentWorkflowId,
  selectAnswerWorkflows,
  writeCurrentWorkflowId,
} from "../workflow/selection";

type Message = {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  abstained?: boolean;
  runId?: string;
};
type Conversation = { id: string; title: string; messages: Message[] };
const STORAGE = "zhiyuan-conversations-v1";

function initialConversations(): Conversation[] {
  try {
    return JSON.parse(localStorage.getItem(STORAGE) || "[]") as Conversation[];
  } catch {
    return [];
  }
}

export default function ChatPage() {
  const [conversations, setConversations] =
    useState<Conversation[]>(initialConversations);
  const [current, setCurrent] = useState<string | null>(null);
  const currentRef = useRef<string | null>(null);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [workflowId, setWorkflowId] = useState("");
  const [workflowLoading, setWorkflowLoading] = useState(true);
  const [topK, setTopK] = useState(5);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [failureNotices, setFailureNotices] = useState<Record<string, string>>({});
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(
    null,
  );
  const fileRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const active = conversations.find((item) => item.id === current);
  const failureNotice = current ? failureNotices[current] : undefined;

  function clearFailure(id: string) {
    setFailureNotices((previous) => {
      if (!(id in previous)) return previous;
      const next = { ...previous };
      delete next[id];
      return next;
    });
  }

  useEffect(() => {
    getWorkflows()
      .then((items) => {
        const answers = selectAnswerWorkflows(items);
        setWorkflows(answers);
        const selected =
          answers.find((item) => item.id === readCurrentWorkflowId())?.id ||
          answers.find((item) => item.id === "medical-answer")?.id ||
          answers[0]?.id ||
          "";
        setWorkflowId(selected);
        writeCurrentWorkflowId(selected);
      })
      .catch((error) => {
        setWorkflows([]);
        setWorkflowId("");
        setNotice(error.message);
      })
      .finally(() => setWorkflowLoading(false));
    getHealth()
      .then((result) => setHealthy(result.milvus))
      .catch(() => setHealthy(false));
  }, []);
  useEffect(() => {
    localStorage.setItem(STORAGE, JSON.stringify(conversations));
  }, [conversations]);
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [active?.messages.length]);

  async function send(value = question) {
    const text = value.trim();
    if (!text || busy || workflowLoading || !workflowId) return;
    const selectedWorkflowId = workflowId;
    const id = currentRef.current || crypto.randomUUID();
    clearFailure(id);
    currentRef.current = id;
    setCurrent(id);
    setConversations((previous) => {
      const exists = previous.some((item) => item.id === id);
      return exists
        ? previous.map((item) =>
            item.id === id
              ? {
                  ...item,
                  messages: [...item.messages, { role: "user", content: text }],
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
    try {
      const result = await runWorkflow(selectedWorkflowId, {
        question: text,
        provider: "qwen",
        top_k: topK,
      });
      if (result.status === "failed") {
        const failure = result.trace.find((item) => item.status === "failed")?.error;
        setFailureNotices((previous) => ({
          ...previous,
          [id]: failure?.message || "回答失败，请检查服务后重试",
        }));
        return;
      }
      if (!result.output.abstained && !result.output.answer?.trim()) {
        setFailureNotices((previous) => ({
          ...previous,
          [id]: "该工作流没有产生回答",
        }));
        return;
      }
      const message: Message = {
        role: "assistant",
        content: result.output.answer || "该工作流没有产生回答",
        citations: result.output.citations || [],
        abstained: result.output.abstained,
      };
      setConversations((previous) =>
        previous.map((item) =>
          item.id === id
            ? { ...item, messages: [...item.messages, message] }
            : item,
        ),
      );
    } catch (error) {
      setFailureNotices((previous) => ({
        ...previous,
        [id]: error instanceof Error ? error.message : "请求失败",
      }));
    } finally {
      setBusy(false);
    }
  }

  async function upload(file: File) {
    setNotice("正在解析并入库文档…");
    try {
      const result = await uploadDocument(file);
      setNotice(
        result.duplicate
          ? "文档已存在，无需重复入库"
          : `已入库 ${result.chunk_count} 个文本块`,
      );
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "上传失败");
    }
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
              }}
            >
              ☰ <span>{item.title}</span>
            </button>
          ))}
        </div>
        <div className="sidebar-bottom">
          <div className="sidebar-card">
            <span className="small-label">工作流引擎</span>
            <strong>让每一步都有迹可循</strong>
            <p>检索、证据门控与引用校验，组成可审查的回答路径。</p>
            <Link to={`/workflows/${workflowId || "medical-answer"}`}>
              查看工作流 ↗
            </Link>
          </div>
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
                提出一个关于公开医疗资料的问题。知源会检索相关内容，核对证据，并将引用呈现在回答旁边。
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
                        <span className="abstain">证据不足</span>
                      )}
                    </div>
                    {message.role === "assistant" ? (
                      <AnswerContent content={message.content} citations={message.citations || []}
                        abstained={!!message.abstained} onCitationClick={setSelectedCitation} />
                    ) : <div className="message-text">{message.content}</div>}
                    {message.role === "assistant" && !message.abstained && !!message.citations?.length && (
                      <div className="citations">
                        <span className="small-label">引用资料</span>
                        {message.citations.map((citation, index) => (
                          <button
                            key={index}
                            type="button"
                            onClick={() => setSelectedCitation(citation)}
                            aria-label={`来源 ${index + 1}：${citation.filename || citation.source || "资料"}`}
                          >
                            [{index + 1}] {citation.filename || citation.source || "资料"}
                            {citation.page ? ` · 第 ${citation.page} 页` : ""}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </article>
              ))}
              {busy && (
                <div className="thinking">
                  <span className="spinner" /> 正在检索并验证证据…
                </div>
              )}
              <div ref={endRef} />
            </div>
          )}
        </div>
        <div className="composer-area">
          <div className="composer-wrap">
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
                <button onClick={() => current && clearFailure(current)} aria-label="关闭错误提示">×</button>
              </div>
            )}
            <div className="settings-bar workflow-selector">
              <label>
                工作流{" "}
                <select
                  aria-label="工作流"
                  value={workflowId}
                  disabled={workflowLoading || !workflows.length}
                  onChange={(event) => {
                    setWorkflowId(event.target.value);
                    writeCurrentWorkflowId(event.target.value);
                  }}
                >
                  {workflows.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name} · v{item.version}
                    </option>
                  ))}
                </select>
              </label>
              {!workflowLoading && !workflows.length && (
                <span className="muted">暂无可用的回答工作流</span>
              )}
            </div>
            <details className="advanced-settings">
              <summary onClick={() => setAdvancedOpen((open) => !open)}>高级设置</summary>
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
              <Link to={`/workflows/${workflowId}`}>编辑此工作流 ↗</Link>
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
                placeholder="向知源提问，了解公开医疗资料…"
                rows={2}
              />
              <div className="composer-actions">
                <button
                  className="attach"
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
                  disabled={busy || workflowLoading || !workflowId || !question.trim()}
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
              {selectedCitation.filename ||
                selectedCitation.source ||
                "公开资料"}
            </h3>
            <p className="citation-meta">
              {selectedCitation.page ? `第 ${selectedCitation.page} 页 · ` : ""}
              {String(selectedCitation.section || "原文摘录")}
            </p>
            <blockquote>{selectedCitation.excerpt || "暂无摘录"}</blockquote>
            <p className="disclaimer">请结合原始资料核查上下文。</p>
          </aside>
        </div>
      )}
    </div>
  );
}
