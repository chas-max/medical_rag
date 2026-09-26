import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  getHealth,
  getWorkflows,
  runWorkflow,
  uploadDocument,
  type Citation,
  type Workflow,
} from "../api/workflows";

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
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [workflowId, setWorkflowId] = useState("medical-answer");
  const [provider, setProvider] = useState("deepseek");
  const [topK, setTopK] = useState(5);
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(
    null,
  );
  const fileRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const active = conversations.find((item) => item.id === current);

  useEffect(() => {
    getWorkflows()
      .then((items) => {
        const answers = items.filter((item) =>
          item.nodes.some((node) => node.type === "answer.output"),
        );
        setWorkflows(answers);
        if (answers.length && !answers.some((item) => item.id === workflowId))
          setWorkflowId(answers[0].id);
      })
      .catch((error) => setNotice(error.message));
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
    if (!text || busy) return;
    const id = current || crypto.randomUUID();
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
      const result = await runWorkflow(workflowId, {
        question: text,
        provider,
        top_k: topK,
      });
      const failure = result.trace.find(
        (item) => item.status === "failed",
      )?.error;
      const message: Message = {
        role: "assistant",
        content:
          result.status === "failed"
            ? failure?.message || "执行失败，请检查工作流和服务状态"
            : result.output.answer || "该工作流没有产生回答",
        citations: result.output.citations || [],
        abstained: result.output.abstained,
        runId: result.run_id,
      };
      setConversations((previous) =>
        previous.map((item) =>
          item.id === id
            ? { ...item, messages: [...item.messages, message] }
            : item,
        ),
      );
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "请求失败");
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
              onClick={() => setCurrent(item.id)}
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
            <Link to={`/workflows/${workflowId}`}>查看工作流 ↗</Link>
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
                    <div className="message-text">{message.content}</div>
                    {!!message.citations?.length && (
                      <div className="citations">
                        <span className="small-label">引用资料</span>
                        {message.citations.map((citation, i) => (
                          <button
                            key={i}
                            onClick={() => setSelectedCitation(citation)}
                          >
                            ◫ &nbsp;
                            {citation.filename || citation.source || "资料"}
                            {citation.page ? ` · 第 ${citation.page} 页` : ""} ↗
                          </button>
                        ))}
                      </div>
                    )}
                    {message.runId && (
                      <span className="run-id">
                        运行记录 #{message.runId.slice(0, 8)}
                      </span>
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
            <div className="settings-bar">
              <label>
                工作流{" "}
                <select
                  aria-label="工作流"
                  value={workflowId}
                  onChange={(event) => setWorkflowId(event.target.value)}
                >
                  {workflows.length ? (
                    workflows.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name} · v{item.version}
                      </option>
                    ))
                  ) : (
                    <option value="medical-answer">医疗资料问答</option>
                  )}
                </select>
              </label>
              <label>
                模型{" "}
                <select
                  aria-label="模型"
                  value={provider}
                  onChange={(event) => setProvider(event.target.value)}
                >
                  <option value="deepseek">DeepSeek</option>
                  <option value="qwen">Qwen</option>
                </select>
              </label>
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
                  disabled={busy || !question.trim()}
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
