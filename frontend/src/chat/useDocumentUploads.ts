import { useCallback, useEffect, useRef, useState } from "react";
import {
  getDocumentJob,
  getDocumentJobs,
  type DocumentJob,
} from "../api/documents";
import { uploadDocument } from "../api/workflows";

const terminal = (job: DocumentJob) =>
  job.status === "succeeded" || job.status === "failed";

/** Server-side jobs survive navigation; network failures never fail a task. */
export function useDocumentUploads() {
  const [jobs, setJobs] = useState<DocumentJob[]>([]);
  const [uploadNotice, setUploadNotice] = useState("");
  const [connectionNotice, setConnectionNotice] = useState("");
  const [uploading, setUploading] = useState(false);
  const tracked = useRef(new Map<string, DocumentJob>());
  const mounted = useRef(false);
  const refreshing = useRef(false);

  const merge = useCallback((incoming: DocumentJob[]) => {
    if (!mounted.current) return;
    for (const job of incoming) {
      const previous = tracked.current.get(job.job_id);
      // A list request started earlier may return an obsolete active snapshot.
      if (!previous || !terminal(previous))
        tracked.current.set(job.job_id, job);
    }
    setJobs([...tracked.current.values()].slice(-50).reverse());
  }, []);

  const refresh = useCallback(async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    try {
      const pending = [...tracked.current.values()].filter(
        (job) => !terminal(job),
      );
      const [active, details] = await Promise.all([
        getDocumentJobs(),
        Promise.all(pending.map((job) => getDocumentJob(job.job_id))),
      ]);
      merge([...active, ...details]);
      if (mounted.current) setConnectionNotice("");
    } catch {
      if (mounted.current)
        setConnectionNotice(
          "任务状态暂时无法连接，将自动重试；已接收的文档仍由后台处理。",
        );
    } finally {
      refreshing.current = false;
    }
  }, [merge]);

  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (!cancelled) timer = setTimeout(() => void poll(), 2000);
    };
    void poll();
    return () => {
      cancelled = true;
      mounted.current = false;
      clearTimeout(timer);
    };
  }, [refresh]);

  const upload = useCallback(
    async (file: File) => {
      setUploading(true);
      setUploadNotice("正在上传并保存文档…");
      try {
        const accepted = await uploadDocument(file);
        merge([
          {
            job_id: accepted.job_id,
            filename: file.name,
            source_id: file.name,
            status: "queued",
            stage: "queued",
            attempts: 0,
            result: null,
            error: null,
            trace: [],
          },
        ]);
        if (mounted.current) setUploadNotice("文档已接收，等待后台处理。");
      } catch (error) {
        if (mounted.current)
          setUploadNotice(error instanceof Error ? error.message : "上传失败");
      } finally {
        if (mounted.current) setUploading(false);
      }
    },
    [merge],
  );

  return { jobs, uploadNotice, connectionNotice, uploading, upload, refresh };
}
