import { useEffect, useRef, useState } from "react";
import {
  clearMemory,
  deleteMemoryEpisode,
  deleteMemoryProfile,
  getMemory,
  getMemoryEpisode,
  saveMemoryProfile,
  setMemoryEnabled,
  type MemoryCategory,
  type MemoryProfile,
  type MemoryState,
} from "../api/memory";

const FIELDS: { key: string; label: string; category: MemoryCategory }[] = [
  { key: "display_name", label: "称呼", category: "background" },
  { key: "occupation", label: "职业背景", category: "background" },
  { key: "language", label: "语言偏好", category: "preferences" },
  { key: "response_style", label: "回答偏好", category: "preferences" },
  { key: "interests", label: "长期关注", category: "interests" },
  { key: "goals", label: "长期目标", category: "interests" },
  { key: "health", label: "健康自述（敏感）", category: "health" },
];
const labelFor = (key: string) =>
  FIELDS.find((field) => field.key === key)?.label ?? key;

/** Local single-user memory controls; Markdown is deliberately rendered as text. */
export default function MemorySection() {
  const [state, setState] = useState<MemoryState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reload, setReload] = useState(0);
  const [key, setKey] = useState("display_name");
  const [value, setValue] = useState("");
  const [sensitive, setSensitive] = useState(false);
  const [armed, setArmed] = useState<string | null>(null);
  const [episode, setEpisode] = useState<{
    id: string;
    content: string;
  } | null>(null);
  const mounted = useRef(false);
  const field = FIELDS.find((item) => item.key === key)!;

  useEffect(() => {
    mounted.current = true;
    let active = true;
    getMemory()
      .then((result) => {
        if (active) {
          setState(result);
          setError("");
        }
      })
      .catch((failure: unknown) => {
        if (active) setError((failure as Error).message);
      });
    return () => {
      active = false;
      mounted.current = false;
    };
  }, [reload]);

  async function change(action: () => Promise<MemoryState>, message: string) {
    if (busy) return false;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await action();
      if (mounted.current) {
        setState(result);
        setArmed(null);
        setEpisode(null);
        setNotice(message);
      }
      return true;
    } catch (failure) {
      if (mounted.current) setError((failure as Error).message);
      return false;
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  function edit(profile: MemoryProfile) {
    setKey(profile.key);
    setValue(profile.value);
    setSensitive(profile.sensitive);
    setArmed(null);
  }

  async function readEpisode(id: string) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await getMemoryEpisode(id);
      if (mounted.current) setEpisode(result);
    } catch (failure) {
      if (mounted.current) setError((failure as Error).message);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <div className="memory-section">
      <p className="section-hint">
        意图分析和情景提取使用当前模型，会增加模型费用和延迟；纯画像管理不检索资料，也不产生情景记忆。
      </p>
      <p className="form-note">
        所有浏览器共享同一个本地用户，没有账号隔离。画像保存在
        SQLite，情景正文保存在 Markdown 文件。
      </p>
      <p className="form-note">
        默认可提取固定字段内非敏感的明确陈述；敏感新增或更正须按次授权。健康自述未经医学核实，不作为诊断或医学证据。删除画像不会删除聊天历史（原始聊天）或情景，也不会自动重学旧聊天。
      </p>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {!state ? (
        <button
          type="button"
          className="button-ghost"
          onClick={() => setReload((n) => n + 1)}
        >
          {error ? "重试读取记忆" : "正在读取记忆…"}
        </button>
      ) : (
        <>
          <label className="memory-toggle">
            <input
              type="checkbox"
              checked={state.enabled}
              disabled={busy}
              onChange={(event) =>
                void change(
                  () => setMemoryEnabled(event.target.checked),
                  "记忆开关已更新",
                )
              }
            />
            启用长期记忆
          </label>
          <p className="form-note">
            关闭后停止召回和自动写入，已有记忆保留；重新开启后可以继续使用。
          </p>
          <h3>用户画像</h3>
          <ul className="profile-list">
            {state.profiles.map((profile) => (
              <li key={profile.key} className="profile-row">
                <span className="profile-main">
                  <strong>{labelFor(profile.key)}</strong>
                  <span>{profile.value}</span>
                  <small>
                    {profile.sensitive ? "敏感 · 用户自述 · " : ""}
                    {profile.source === "manual" ? "人工填写" : "来自用户原文"}
                    {profile.support ? ` · ${profile.support}` : ""}
                  </small>
                </span>
                <span className="profile-actions">
                  <button
                    type="button"
                    className="button-ghost"
                    disabled={busy}
                    aria-label={`编辑${labelFor(profile.key)}`}
                    onClick={() => edit(profile)}
                  >
                    编辑
                  </button>
                  <button
                    type="button"
                    className={
                      armed === `profile:${profile.key}`
                        ? "button-danger"
                        : "button-ghost"
                    }
                    disabled={busy}
                    aria-label={`${armed === `profile:${profile.key}` ? "确认删除" : "删除"}${labelFor(profile.key)}`}
                    onClick={() =>
                      armed === `profile:${profile.key}`
                        ? void change(
                            () => deleteMemoryProfile(profile.key),
                            "画像已删除；聊天历史保留",
                          )
                        : setArmed(`profile:${profile.key}`)
                    }
                  >
                    {armed === `profile:${profile.key}` ? "确认删除" : "删除"}
                  </button>
                </span>
              </li>
            ))}
            {!state.profiles.length && (
              <li className="profile-empty">暂无用户画像</li>
            )}
          </ul>
          <form
            className="model-config memory-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!value.trim() || (key === "health" && !sensitive)) return;
              void change(
                () =>
                  saveMemoryProfile(key, {
                    value: value.trim(),
                    category: field.category,
                    sensitive,
                  }),
                "画像已保存",
              ).then((saved) => {
                if (saved && mounted.current) {
                  setValue("");
                  setSensitive(false);
                }
              });
            }}
          >
            <label>
              画像字段
              <select
                value={key}
                disabled={busy}
                onChange={(event) => {
                  setKey(event.target.value);
                  setValue("");
                  setSensitive(false);
                }}
              >
                {FIELDS.map((item) => (
                  <option value={item.key} key={item.key}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              画像内容
              <textarea
                value={value}
                maxLength={500}
                disabled={busy}
                onChange={(event) => setValue(event.target.value)}
                rows={2}
              />
            </label>
            <label className="memory-toggle">
              <input
                type="checkbox"
                checked={sensitive}
                disabled={busy}
                onChange={(event) => setSensitive(event.target.checked)}
              />
              我同意保存此条敏感信息
            </label>
            <button
              type="submit"
              className="button-primary"
              disabled={
                busy || !value.trim() || (key === "health" && !sensitive)
              }
            >
              保存画像
            </button>
          </form>
          <h3>情景记忆（Markdown）</h3>
          <ul className="profile-list">
            {state.episodes.map((item) => (
              <li key={item.id} className="profile-row">
                <span className="profile-main">
                  <strong>{item.title}</strong>
                  <small>
                    {item.created_at}
                    {item.sensitive ? " · 敏感 · 用户授权" : ""}
                  </small>
                </span>
                <span className="profile-actions">
                  <button
                    type="button"
                    className="button-ghost"
                    disabled={busy}
                    aria-label={`阅读${item.title}`}
                    onClick={() => void readEpisode(item.id)}
                  >
                    阅读
                  </button>
                  <button
                    type="button"
                    className={
                      armed === `episode:${item.id}`
                        ? "button-danger"
                        : "button-ghost"
                    }
                    disabled={busy}
                    aria-label={`${armed === `episode:${item.id}` ? "确认删除情景" : "删除情景"}${item.title}`}
                    onClick={() =>
                      armed === `episode:${item.id}`
                        ? void change(
                            () => deleteMemoryEpisode(item.id),
                            "情景正文已删除；聊天历史保留",
                          )
                        : setArmed(`episode:${item.id}`)
                    }
                  >
                    {armed === `episode:${item.id}` ? "确认删除" : "删除"}
                  </button>
                </span>
              </li>
            ))}
            {!state.episodes.length && (
              <li className="profile-empty">暂无情景记忆</li>
            )}
          </ul>
          {episode && (
            <div>
              <button
                type="button"
                className="button-ghost"
                onClick={() => setEpisode(null)}
              >
                关闭情景正文
              </button>
              <pre className="memory-markdown" aria-label="情景 Markdown 正文">
                {episode.content}
              </pre>
            </div>
          )}
          <button
            type="button"
            className="button-danger"
            disabled={busy}
            onClick={() =>
              armed === "clear"
                ? void change(clearMemory, "全部记忆已清空；聊天历史保留")
                : setArmed("clear")
            }
          >
            {armed === "clear" ? "确认清空全部记忆" : "清空全部记忆"}
          </button>
          {armed && (
            <p className="form-note" role="status">
              此操作会删除记忆，无法撤销；不会删除聊天历史。请再次确认。
              <button
                type="button"
                className="button-ghost"
                onClick={() => setArmed(null)}
              >
                取消删除
              </button>
            </p>
          )}
        </>
      )}
    </div>
  );
}
