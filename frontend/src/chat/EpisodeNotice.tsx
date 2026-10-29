import { useEffect, useState } from "react";
import { getMemoryJob } from "../api/memory";
import type { EpisodeOutcome } from "../api/workflows";

/** One bounded polling loop per actual episode job; profiles never enter this path. */
export function EpisodeNotice({
  runId,
  status,
  onStatus,
}: {
  runId?: string;
  status: string;
  onStatus: (status: EpisodeOutcome["status"]) => void;
}) {
  const [localStatus, setLocalStatus] = useState(status);
  const pending = status === "pending" || status === "processing";
  useEffect(() => {
    if (!runId || !pending) return;
    let active = true;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function poll() {
      try {
        const job = await getMemoryJob(runId!, controller.signal);
        if (!active) return;
        setLocalStatus(job.status);
        if (job.status === "pending" || job.status === "processing") {
          attempts += 1;
          if (attempts < 40) timer = setTimeout(() => void poll(), 1000);
          else setLocalStatus("delayed");
        } else onStatus(job.status);
      } catch {
        if (active) setLocalStatus("unavailable");
      }
    }
    timer = setTimeout(() => void poll(), 1000);
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
    // The callback updates the parent message; its identity must not restart the budget.
  }, [runId, pending]);
  const displayed = pending ? localStatus : status;
  if (displayed === "skipped") return null;
  return (
    <p className="memory-status" role="status">
      {displayed === "saved"
        ? "情景记忆已保存"
        : displayed === "failed"
          ? "情景记忆保存失败；回答和聊天历史不受影响"
          : displayed === "pending" || displayed === "processing"
            ? "情景记忆正在提取，尚不能保证下一轮可召回"
            : displayed === "cancelled"
              ? "本轮情景记忆任务已取消"
              : displayed === "delayed"
                ? "情景记忆仍在后台处理，请稍后到设置查看"
                : "情景记忆状态暂不可用；回答和聊天历史不受影响"}
    </p>
  );
}
