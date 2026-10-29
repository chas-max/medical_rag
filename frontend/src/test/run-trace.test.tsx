import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import * as trace from "../workflow/runTrace";
import RunTrace from "../components/RunTrace";

afterEach(cleanup);
const nested = [
  {
    node_id: "container",
    status: "success",
    duration_ms: 123,
    error: null,
    metadata: {
      api_key: "SECRET",
      thoughts: "PRIVATE",
      agentic: {
        stop_reason: "sufficient",
        retrieval_rounds: 1,
        model_calls: 3,
        raw_response: "RAW",
        iterations: [
          {
            iteration: 1,
            queries: ["补充查询"],
            evidence_ids: ["chunk-1"],
            duration_ms: 50,
            assessment: {
              sufficient: true,
              missing: [],
              supporting_chunk_ids: ["chunk-1"],
            },
            trace: [
              {
                node_id: "bm25",
                status: "success",
                duration_ms: 10,
                metadata: { secret: "SECRET" },
                error: null,
              },
            ],
          },
        ],
      },
    },
  },
];
it("shows expandable nested safe trace, queries, assessment IDs, status, durations and stop reason", () => {
  render(<RunTrace trace={nested} runId="run-1" />);
  expect(screen.getByText(/运行轨迹/)).toBeInTheDocument();
  expect(screen.getByText(/sufficient/)).toBeInTheDocument();
  expect(screen.getByText(/第 1 轮/)).toBeInTheDocument();
  expect(screen.getByText(/补充查询/)).toBeInTheDocument();
  expect(screen.getByText(/bm25/)).toHaveTextContent("success · 10 ms");
  expect(screen.getByText(/评估: 证据充分/)).toHaveTextContent("chunk-1");
  expect(document.body.textContent).not.toMatch(/SECRET|PRIVATE|RAW/);
});
it("normalizes old, empty and malformed metadata without serializing private fields", () => {
  expect(trace.safeRunTrace?.(null)).toEqual([]);
  const clean = trace.safeRunTrace?.([
    ...nested,
    null,
    { node_id: "old", status: "inactive", metadata: [] },
    {
      node_id: "bad",
      status: "failed",
      duration_ms: -1,
      metadata: {
        agentic: {
          iterations: [
            null,
            { queries: "bad", evidence_ids: [false], trace: 4 },
          ],
          stop_reason: {},
          model_calls: "private",
        },
      },
      error: { code: "TIMEOUT", message: "SECRET" },
    },
  ]);
  expect(JSON.stringify(clean)).not.toMatch(/SECRET|PRIVATE|RAW|message/);
  render(<RunTrace trace={clean} />);
  expect(screen.getByText(/old/)).toBeInTheDocument();
  expect(screen.getByText(/TIMEOUT/)).toBeInTheDocument();
});
it("omits a trace panel for old chats without trace", () => {
  render(<RunTrace trace={undefined} />);
  expect(screen.queryByText(/运行轨迹/)).not.toBeInTheDocument();
});

it("preserves backend halt and skipped statuses", () => {
  expect(
    trace
      .safeRunTrace([
        { node_id: "gate", status: "halt" },
        { node_id: "repair", status: "skipped" },
      ])
      .map((node) => node.status),
  ).toEqual(["halt", "skipped"]);
});

it("keeps persisted failed attempt traces safe and stable on reload", () => {
  const failed = trace.safeRunTrace([
    {
      node_id: "failed-child",
      status: "failed",
      duration_ms: 7,
      error: { code: "TIMEOUT", message: "PRIVATE" },
      metadata: { thoughts: "SECRET" },
    },
  ]);
  const reloaded = trace.safeRunTrace(JSON.parse(JSON.stringify(failed)));
  expect(reloaded).toEqual(failed);
  render(
    <>
      <RunTrace trace={reloaded} runId="first-failure" />
      <RunTrace trace={reloaded} runId="second-failure" />
    </>,
  );
  expect(screen.getByText("运行轨迹 · first-failure")).toBeInTheDocument();
  expect(screen.getByText("运行轨迹 · second-failure")).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/SECRET|PRIVATE/);
});
