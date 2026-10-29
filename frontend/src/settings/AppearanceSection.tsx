import { themes, setTheme, useTheme } from "./theme";

/**
 * 主题选择。每张卡片内嵌一个迷你界面示意，直接用对应主题的
 * CSS 变量着色（见 styles.css 里 .theme-preview 的变量复用），所见即所得。
 */
export default function AppearanceSection() {
  const active = useTheme();
  return (
    <div className="model-config-wrap">
      <p className="section-hint">
        主题会立即生效并保存在这个浏览器里，刷新后保持不变。三种主题都做了整站配色协调：
        面板、侧栏、输入框和强调色各司其职，不会只靠黑白两色区分层级。
      </p>
      <div className="theme-grid" role="radiogroup" aria-label="界面主题">
        {themes.map((theme) => (
          <button
            key={theme.id}
            type="button"
            role="radio"
            aria-checked={active === theme.id}
            className={active === theme.id ? "theme-card active" : "theme-card"}
            onClick={() => setTheme(theme.id)}
          >
            <span
              className="theme-preview"
              data-theme-preview={theme.id}
              aria-hidden="true"
            >
              <span className="tp-top">
                <span className="tp-dot" />
                <span className="tp-line" />
              </span>
              <span className="tp-body">
                <span className="tp-side" />
                <span className="tp-main">
                  <span className="tp-bubble user" />
                  <span className="tp-bubble" />
                  <span className="tp-chip" />
                </span>
              </span>
            </span>
            <span className="theme-card-text">
              <strong>{theme.label}</strong>
              <small>{theme.hint}</small>
            </span>
          </button>
        ))}
      </div>
      <p className="form-note">
        当前使用「{themes.find((theme) => theme.id === active)?.label}」主题。切换后问答页与工作流编辑器会一起换色。
      </p>
    </div>
  );
}
