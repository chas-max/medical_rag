import { useEffect, useRef } from "react";
import { roles, type Identity } from "./model";

type Props = {
  me: Identity;
  open: boolean;
  disabled: boolean;
  onToggle: () => void;
  onClose: () => void;
  onLogout: () => void;
};

/**
 * Bottom-left account control, modelled on chat products: the avatar and role
 * stay visible, and the account facts plus the only terminal action live in a
 * small dialog. Nothing sensitive is stored outside component memory.
 */
export function AccountMenu({
  me,
  open,
  disabled,
  onToggle,
  onClose,
  onLogout,
}: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const click = (event: MouseEvent) => {
      if (
        wrap.current &&
        event.target instanceof Node &&
        !wrap.current.contains(event.target)
      )
        onClose();
    };
    document.addEventListener("keydown", key);
    document.addEventListener("mousedown", click);
    return () => {
      document.removeEventListener("keydown", key);
      document.removeEventListener("mousedown", click);
    };
  }, [open, onClose]);
  const label =
    me.roles.map((role) => roles[role] ?? "未开放角色").join("、") ||
    "未分配角色";
  return (
    <div className="patient-account" ref={wrap}>
      {open && (
        <div className="patient-account-pop" role="dialog" aria-label="账户信息">
          <p className="patient-account-name">{label}</p>
          <dl>
            <dt>本系统账户</dt>
            <dd>{me.user_id}</dd>
            <dt>所属机构</dt>
            <dd>{me.tenant_id}</dd>
            <dt>身份核验</dt>
            <dd>{me.binding.verified ? "已核验" : "未核验"}</dd>
          </dl>
          <p className="patient-account-note">
            系统角色声明不等于医院身份核验。离开或隐藏页面会清空本页敏感内容。
          </p>
          <button
            type="button"
            className="patient-secondary"
            disabled={disabled}
            onClick={onLogout}
          >
            退出登录
          </button>
        </div>
      )}
      <button
        type="button"
        className="patient-account-trigger"
        aria-label="账户菜单"
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={disabled}
        onClick={onToggle}
      >
        <span className="patient-avatar" aria-hidden="true">
          {label.slice(0, 1)}
        </span>
        <span className="patient-account-meta">
          <strong>{label}</strong>
          <small>{me.user_id}</small>
        </span>
        <span className="patient-account-caret" aria-hidden="true">
          ⌃
        </span>
      </button>
    </div>
  );
}
