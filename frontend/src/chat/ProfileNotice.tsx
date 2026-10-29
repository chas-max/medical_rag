import { useEffect, useRef, useState } from "react";
import type { ProfileOutcome } from "../api/workflows";
import { ApiRequestError } from "../api/client";

export function ProfileNotice({
  outcome,
  onConfirm,
  disabled,
}: {
  outcome: ProfileOutcome;
  onConfirm: (accept: boolean) => Promise<void>;
  disabled: boolean;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [expired, setExpired] = useState(false);
  const [terminal, setTerminal] = useState(false);
  const lock = useRef(false);
  const expires = outcome.confirmation_expires_at;
  useEffect(() => {
    setError("");
    setTerminal(false);
    setExpired(!!expires && expires * 1000 <= Date.now());
    if (!expires) return;
    const timer = setTimeout(
      () => setExpired(true),
      Math.max(0, expires * 1000 - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [outcome.confirmation_token, expires]);
  async function confirm(accept: boolean) {
    if (disabled || lock.current || expired || terminal) return;
    lock.current = true;
    setPending(true);
    setError("");
    try {
      await onConfirm(accept);
    } catch (failure) {
      if (
        failure instanceof ApiRequestError &&
        failure.status === 409 &&
        [
          "MEMORY_CONFIRMATION_EXPIRED",
          "MEMORY_CONFIRMATION_CONFLICT",
          "MEMORY_CONFIRMATION_INVALID",
        ].includes(failure.code || "")
      ) {
        setTerminal(true);
        setError(failure.message);
      } else setError("画像确认失败，请检查连接后重试");
    } finally {
      lock.current = false;
      setPending(false);
    }
  }
  return (
    <div className="profile-notice" role="status">
      {outcome.actions.map((action, index) => (
        <p key={`${action.key}-${index}`}>{action.message}</p>
      ))}
      {!outcome.actions.length && outcome.status === "disabled" && (
        <p>画像记忆已关闭</p>
      )}
      {!outcome.actions.length && outcome.status === "failed" && (
        <p>画像处理失败；回答状态单独显示</p>
      )}
      {outcome.confirmation_token &&
        !terminal &&
        (expired ? (
          <p>画像确认已到期，请重新提出变更。</p>
        ) : (
          <div className="profile-confirmation">
            {outcome.confirmation_requires_consent && (
              <p className="profile-consent-note">
                此变更包含敏感信息，默认不保存；如需记录请到设置页「长期记忆」手动添加。
              </p>
            )}
            {!outcome.confirmation_requires_consent && (
              <button
                type="button"
                className="button-ghost"
                disabled={disabled || pending}
                onClick={() => void confirm(true)}
              >
                确认画像变更
              </button>
            )}
            <button
              type="button"
              className="button-ghost"
              disabled={disabled || pending}
              onClick={() => void confirm(false)}
            >
              取消画像变更
            </button>
          </div>
        ))}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
