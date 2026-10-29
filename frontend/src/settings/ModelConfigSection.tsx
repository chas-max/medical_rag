import { useEffect, useState } from "react";
import {
  deleteModelProfile,
  getModelConfig,
  saveModelConfig,
  selectModelProfile,
  type ModelConfig,
  type ModelConfigSource,
  type ModelProfile,
} from "../api/settings";

type Status = { kind: "loading" | "error" | "success"; message: string } | null;

const SOURCE_LABEL: Record<ModelConfigSource, string> = {
  settings: "来自模型配置",
  env: "来自 .env",
  none: "尚未配置",
};

/** Enough of the address to tell two gateways apart without wrapping the row. */
function host(url: string): string {
  return url.split("//")[1]?.split("/")[0] ?? url;
}

function verifiedAgo(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 1) return "刚刚校验";
  if (minutes < 60) return `${minutes} 分钟前校验`;
  if (minutes < 1440) return `${Math.round(minutes / 60)} 小时前校验`;
  return `${Math.round(minutes / 1440)} 天前校验`;
}

export default function ModelConfigSection() {
  const [config, setConfig] = useState<ModelConfig | null>(null);
  const [modelName, setModelName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [armed, setArmed] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>(null);

  useEffect(() => {
    let active = true;
    getModelConfig()
      .then((value) => {
        if (!active) return;
        setConfig(value);
        setModelName(value.model_name);
        setBaseUrl(value.base_url);
      })
      .catch((error: unknown) => {
        if (active) setStatus({ kind: "error", message: (error as Error).message });
      });
    return () => {
      active = false;
    };
  }, []);

  const complete = Boolean(modelName.trim() && baseUrl.trim() && apiKey.trim());
  const locked = busy || pending !== null;

  async function confirm() {
    if (locked) return;
    if (!complete) {
      setStatus({ kind: "error", message: "模型名称、Base URL 和 API Key 都需要填写，请补齐后重试。" });
      return;
    }
    setBusy(true);
    setStatus({ kind: "loading", message: "正在向该模型服务自检配置…" });
    try {
      const saved = await saveModelConfig({
        model_name: modelName.trim(),
        base_url: baseUrl.trim(),
        api_key: apiKey.trim(),
      });
      setConfig(saved);
      // The server keeps the key; leaving it in the DOM would be the safer-looking but worse choice.
      setApiKey("");
      setStatus({ kind: "success", message: `已保存并切换到 ${saved.model_name}` });
    } catch (error: unknown) {
      setStatus({ kind: "error", message: `${(error as Error).message}（当前模型未变）` });
    } finally {
      setBusy(false);
    }
  }

  async function switchTo(profile: ModelProfile) {
    if (locked) return;
    setPending(profile.id);
    try {
      setConfig(await selectModelProfile(profile.id));
      setModelName(profile.model_name);
      setBaseUrl(profile.base_url);
      setStatus({ kind: "success", message: `已切换到 ${profile.model_name}，未再发起校验` });
    } catch (error: unknown) {
      setStatus({ kind: "error", message: (error as Error).message });
    } finally {
      setPending(null);
    }
  }

  async function remove(profile: ModelProfile) {
    if (locked) return;
    if (armed !== profile.id) {
      setArmed(profile.id);
      setStatus({
        kind: "error",
        message: profile.active
          ? `再点一次“确认移除”会删掉正在使用的 ${profile.model_name}，并回落到 .env 配置。`
          : `再点一次“确认移除”会删掉 ${profile.model_name} 的已存密钥。`,
      });
      return;
    }
    setPending(profile.id);
    try {
      setConfig(await deleteModelProfile(profile.id));
      setArmed(null);
      setStatus({ kind: "success", message: `已移除 ${profile.model_name}` });
    } catch (error: unknown) {
      setStatus({ kind: "error", message: (error as Error).message });
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="model-config-wrap">
      <p className="section-hint">
        支持任意 OpenAI 兼容服务（Qwen、DeepSeek、OpenAI、vLLM、Ollama 等）。回答、问题路由与重写都走当前选中的那一个模型，切换与新增都即时生效，无需重启后端。
      </p>
      <div className="current-config">
        <span className="small-label">当前生效</span>
        <strong>{config ? `${config.model_name} · ${host(config.base_url)}` : "读取中…"}</strong>
        <em>{config ? SOURCE_LABEL[config.source] : ""}</em>
      </div>
      {config && (
        <ul className="profile-list">
          {config.profiles.map((profile) => (
            <li key={profile.id} className={profile.active ? "profile-row active" : "profile-row"}>
              <span className="profile-main">
                <strong>{profile.model_name}</strong>
                <small>
                  {host(profile.base_url)} · {profile.api_key_masked}
                  {profile.verified_at ? ` · ${verifiedAgo(profile.verified_at)}` : ""}
                </small>
              </span>
              <span className="profile-actions">
                {profile.active ? (
                  <span className="badge">使用中</span>
                ) : (
                  <button
                    type="button"
                    className="button-ghost"
                    disabled={locked}
                    onClick={() => void switchTo(profile)}
                  >
                    {pending === profile.id ? "切换中…" : "切换"}
                  </button>
                )}
                <button
                  type="button"
                  className={armed === profile.id ? "button-danger" : "button-ghost"}
                  disabled={locked}
                  onClick={() => void remove(profile)}
                >
                  {armed === profile.id ? "确认移除" : "移除"}
                </button>
              </span>
            </li>
          ))}
          {!config.profiles.length && (
            <li className="profile-empty">
              还没有保存过的模型。确认后这里会列出每一项，随时切回。
              {config.fallback.configured ? " 移除全部配置即回到 .env 的默认模型。" : ""}
            </li>
          )}
        </ul>
      )}
      <form
        className="model-config"
        onSubmit={(event) => {
          event.preventDefault();
          void confirm();
        }}
      >
        <span className="small-label">新增模型 / 更新当前项</span>
        <label>
          模型名称
          <input
            value={modelName}
            onChange={(event) => setModelName(event.target.value)}
            placeholder="qwen3.8-flash / deepseek-chat / gpt-4o-mini"
            autoComplete="off"
          />
        </label>
        <label>
          Base URL
          <input
            value={baseUrl}
            onChange={(event) => setBaseUrl(event.target.value)}
            placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <label>
          API Key
          <input
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={config?.api_key_masked || "sk-…"}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        {status && (
          <p
            className={status.kind === "error" ? "notice error" : "notice"}
            role={status.kind === "error" ? "alert" : "status"}
          >
            {status.message}
          </p>
        )}
        <div className="model-config-actions">
          <button type="submit" className="button-primary" disabled={busy}>
            {busy ? "校验中…" : "确认并生效"}
          </button>
          {!complete && <span className="form-note">三项都填好后才能确认</span>}
        </div>
        <p className="form-note">
          确认时后端会真的向该服务发一次最小请求，通过才保存并切换。模型名与 Base URL
          都相同的那条会被更新（比如只换 Key），否则新增一条。密钥以明文存在
          settings.json，文件已被 git 忽略。模型名以 qwen/qwq 开头或地址属于 DashScope
          时会自动带上<code> enable_thinking=false </code>，其他服务不会收到该参数。
        </p>
      </form>
    </div>
  );
}
