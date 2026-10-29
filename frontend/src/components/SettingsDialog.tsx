import { useEffect, useState } from "react";
import { defaultSectionId, settingsSections } from "../settings/registry";

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function SettingsDialog({ open, onClose }: Props) {
  const [sectionId, setSectionId] = useState(defaultSectionId);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const section =
    settingsSections.find((item) => item.id === sectionId) ?? settingsSections[0];
  const Pane = section.component;

  return (
    <div
      className="settings-overlay"
      role="presentation"
      onClick={onClose}
    >
      <div
        className="settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="设置"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-head">
          <span className="eyebrow">SETTINGS · 设置</span>
          <button className="settings-close" onClick={onClose} aria-label="关闭设置">
            ×
          </button>
        </header>
        <div className="settings-body">
          <nav aria-label="设置项" className="settings-nav">
            {settingsSections.map((item) => (
              <button
                key={item.id}
                type="button"
                className={item.id === section.id ? "active" : ""}
                aria-current={item.id === section.id ? "true" : undefined}
                onClick={() => setSectionId(item.id)}
              >
                <span className="settings-nav-icon">{item.icon}</span>
                <span className="settings-nav-text">
                  <strong>{item.label}</strong>
                  <small>{item.hint}</small>
                </span>
              </button>
            ))}
          </nav>
          <section className="settings-pane">
            <h2>{section.label}</h2>
            <Pane />
          </section>
        </div>
      </div>
    </div>
  );
}
