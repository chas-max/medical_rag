import { request } from "./client";

export type DocumentJobStatus =
  "queued" | "running" | "retrying" | "succeeded" | "failed";

export interface DocumentSubmission {
  job_id: string;
  status: "queued";
  status_url: string;
}

export interface DocumentJob {
  job_id: string;
  filename: string;
  source_id: string;
  status: DocumentJobStatus;
  stage: string;
  attempts: number;
  result: {
    document_id: string;
    chunk_count: number;
    strategy: string;
    reason: string;
    duplicate: boolean;
  } | null;
  error: { code: string; message: string } | null;
  trace: unknown[];
}

export const getDocumentJob = (jobId: string) =>
  request<DocumentJob>(`/document-jobs/${encodeURIComponent(jobId)}`);

export const getDocumentJobs = (active = true, limit = 50) =>
  request<DocumentJob[]>(`/document-jobs?active=${active}&limit=${limit}`);
