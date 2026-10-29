import type { DocumentJob } from "../api/documents";

const stages: Record<string, string> = {
  queued: "已接收，等待后台处理",
  pending: "已接收，等待后台处理",
  parse: "正在解析",
  chunk: "正在分块",
  embed: "正在向量化",
  store: "正在入库",
};

function description(job: DocumentJob): string {
  if (job.status === "failed")
    return job.error?.message || "入库失败，请重新上传";
  if (job.status === "succeeded")
    return job.result?.duplicate
      ? "文档已存在，无需重复入库"
      : `已入库 ${job.result?.chunk_count ?? 0} 个文本块`;
  if (job.status === "retrying") return "暂时失败，等待自动重试";
  return stages[job.stage] || "正在后台处理";
}

export function DocumentJobList({ jobs }: { jobs: DocumentJob[] }) {
  if (!jobs.length) return null;
  return (
    <section
      aria-label="文档入库任务"
      aria-live="polite"
      className="document-jobs"
    >
      {jobs.map((job) => (
        <div
          key={job.job_id}
          className={`notice${job.status === "failed" ? " error" : ""}`}
        >
          <span>
            {job.filename}：{description(job)}
          </span>
        </div>
      ))}
    </section>
  );
}
