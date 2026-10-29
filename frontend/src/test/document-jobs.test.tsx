import {
  act,
  cleanup,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as jobsApi from "../api/documents";
import * as workflows from "../api/workflows";
import { useDocumentUploads } from "../chat/useDocumentUploads";
import { DocumentJobList } from "../chat/DocumentJobList";

vi.mock("../api/documents", () => ({
  getDocumentJob: vi.fn(),
  getDocumentJobs: vi.fn(),
}));
vi.mock("../api/workflows", () => ({ uploadDocument: vi.fn() }));

const queued: jobsApi.DocumentJob = {
  job_id: "upload-1",
  filename: "guide.md",
  source_id: "guide.md",
  status: "queued",
  stage: "queued",
  attempts: 0,
  result: null,
  error: null,
  trace: [],
};

beforeEach(() => {
  vi.mocked(jobsApi.getDocumentJobs).mockResolvedValue([]);
  vi.mocked(jobsApi.getDocumentJob).mockResolvedValue(queued);
  vi.mocked(workflows.uploadDocument).mockResolvedValue({
    job_id: queued.job_id,
    status: "queued",
    status_url: "/document-jobs/upload-1",
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
});

it("keeps accepted uploads pending until the persisted task succeeds", async () => {
  const { result } = renderHook(() => useDocumentUploads());
  await waitFor(() => expect(jobsApi.getDocumentJobs).toHaveBeenCalled());
  await act(async () => {
    await result.current.upload(new File(["指南"], "guide.md"));
  });
  expect(result.current.jobs[0].status).toBe("queued");
  expect(result.current.uploadNotice).toContain("已接收");
  vi.mocked(jobsApi.getDocumentJob).mockResolvedValue({
    ...queued,
    status: "succeeded",
    stage: "completed",
    attempts: 1,
    result: {
      document_id: "doc-1",
      chunk_count: 4,
      duplicate: false,
      strategy: "markdown",
      reason: "",
    },
  });
  vi.useFakeTimers();
  await act(async () => {
    await result.current.refresh();
  });
  expect(result.current.jobs[0].result?.chunk_count).toBe(4);
  expect(result.current.jobs[0].status).toBe("succeeded");
});

it("recovers server-side active uploads after page reload and polls every two seconds", async () => {
  vi.useFakeTimers();
  vi.mocked(jobsApi.getDocumentJobs).mockResolvedValue([queued]);
  const { result } = renderHook(() => useDocumentUploads());
  await act(async () => {
    await Promise.resolve();
  });
  expect(result.current.jobs[0].job_id).toBe("upload-1");
  vi.mocked(jobsApi.getDocumentJob).mockResolvedValue({
    ...queued,
    status: "running",
    stage: "embed",
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(result.current.jobs[0].stage).toBe("embed");
});

it("does not mark a task failed on a polling outage and resumes after reconnect", async () => {
  vi.mocked(jobsApi.getDocumentJobs).mockResolvedValue([queued]);
  const { result } = renderHook(() => useDocumentUploads());
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  vi.mocked(jobsApi.getDocumentJob).mockRejectedValue(new Error("断网"));
  await act(async () => {
    await result.current.refresh();
  });
  expect(result.current.jobs[0].status).toBe("queued");
  expect(result.current.connectionNotice).toContain("重试");
  vi.mocked(jobsApi.getDocumentJob).mockResolvedValue({
    ...queued,
    status: "failed",
    error: { code: "INVALID_DOCUMENT", message: "PDF 无文字" },
  });
  await act(async () => {
    await result.current.refresh();
  });
  expect(result.current.connectionNotice).toBe("");
  expect(result.current.jobs[0].error?.message).toBe("PDF 无文字");
});

it("does not allow a stale active-list response to undo a terminal task result", async () => {
  vi.mocked(jobsApi.getDocumentJobs).mockResolvedValue([queued]);
  const { result } = renderHook(() => useDocumentUploads());
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  vi.mocked(jobsApi.getDocumentJob).mockResolvedValue({
    ...queued,
    status: "succeeded",
  });
  await act(async () => {
    await result.current.refresh();
  });
  await act(async () => {
    await result.current.refresh();
  });
  expect(result.current.jobs[0].status).toBe("succeeded");
});

it("reports rejected uploads separately from already accepted background work", async () => {
  vi.mocked(jobsApi.getDocumentJobs).mockResolvedValue([queued]);
  const { result } = renderHook(() => useDocumentUploads());
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  vi.mocked(workflows.uploadDocument).mockRejectedValue(new Error("文件过大"));
  await act(async () => {
    await result.current.upload(new File(["x"], "large.md"));
  });
  expect(result.current.uploadNotice).toBe("文件过大");
  expect(result.current.jobs[0].status).toBe("queued");
});

it("renders processing stages, duplicate results and actionable failures", () => {
  render(
    <DocumentJobList
      jobs={[
        { ...queued, status: "running", stage: "embed" },
        {
          ...queued,
          job_id: "duplicate",
          status: "succeeded",
          result: {
            document_id: "doc",
            chunk_count: 2,
            duplicate: true,
            strategy: "markdown",
            reason: "",
          },
        },
        {
          ...queued,
          job_id: "failed",
          status: "failed",
          error: { code: "INVALID_DOCUMENT", message: "文件没有可提取文字" },
        },
      ]}
    />,
  );
  expect(screen.getByText(/正在向量化/)).toBeInTheDocument();
  expect(screen.getByText(/文档已存在/)).toBeInTheDocument();
  expect(screen.getByText(/文件没有可提取文字/)).toBeInTheDocument();
});

it("stops scheduling polls when the view unmounts", async () => {
  vi.useFakeTimers();
  const view = renderHook(() => useDocumentUploads());
  await act(async () => { await Promise.resolve(); });
  view.unmount();
  const calls = vi.mocked(jobsApi.getDocumentJobs).mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(jobsApi.getDocumentJobs).toHaveBeenCalledTimes(calls);
});
