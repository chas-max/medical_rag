import type { NodeProgressPayload } from "../api/workflows";

export interface SafeNodeTrace {
  node_id: string;
  status: string;
  duration_ms: number;
  error: { code: string } | null;
  agentic?: {
    stop_reason: string;
    retrieval_rounds: number;
    model_calls: number;
    iterations: {
      iteration: number;
      queries: string[];
      evidence_ids: string[];
      duration_ms: number;
      trace: SafeNodeTrace[];
      assessment?: {
        sufficient: boolean;
        missing: string[];
        supporting_chunk_ids: string[];
      };
    }[];
  };
}
const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const count = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
const stops = [
  "sufficient",
  "max_rounds",
  "repeated_query",
  "no_new_evidence",
  "timeout",
  "assessment_failed",
  "planning_failed",
  "retrieval_failed",
];
const stop = (value: unknown) =>
  typeof value === "string" && stops.includes(value) ? value : "unknown";

/** Allow-list the public audit contract, never persist arbitrary metadata or error text. */
export function safeRunTrace(value: unknown, nested = false): SafeNodeTrace[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const node = object(item);
    if (typeof node.node_id !== "string") return [];
    const error = object(node.error);
    const clean: SafeNodeTrace = {
      node_id: node.node_id,
      status: [
        "success",
        "failed",
        "inactive",
        "running",
        "halt",
        "skipped",
      ].includes(String(node.status))
        ? String(node.status)
        : "unknown",
      duration_ms: count(node.duration_ms),
      error:
        typeof error.code === "string" && /^[A-Z_]+$/.test(error.code)
          ? { code: error.code }
          : null,
    };
    const report = object(object(node.metadata).agentic ?? node.agentic);
    if (!nested && Object.keys(report).length) {
      clean.agentic = {
        stop_reason: stop(report.stop_reason),
        retrieval_rounds: count(report.retrieval_rounds),
        model_calls: count(report.model_calls),
        iterations: Array.isArray(report.iterations)
          ? report.iterations.flatMap((value) => {
              const round = object(value);
              if (!Object.keys(round).length) return [];
              const assessment = object(round.assessment);
              return [
                {
                  iteration: count(round.iteration),
                  queries: strings(round.queries),
                  evidence_ids: strings(round.evidence_ids),
                  duration_ms: count(round.duration_ms),
                  trace: safeRunTrace(round.trace, true),
                  ...(typeof assessment.sufficient === "boolean"
                    ? {
                        assessment: {
                          sufficient: assessment.sufficient,
                          missing: strings(assessment.missing),
                          supporting_chunk_ids: strings(
                            assessment.supporting_chunk_ids,
                          ),
                        },
                      }
                    : {}),
                },
              ];
            })
          : [],
      };
    }
    return [clean];
  });
}

export function safeProgress(value: unknown): NodeProgressPayload | null {
  const progress = object(value);
  if (
    typeof progress.node_id !== "string" ||
    !["assessing", "planning", "retrieving", "stopped"].includes(
      String(progress.stage),
    )
  )
    return null;
  return {
    node_id: progress.node_id,
    stage: progress.stage as NodeProgressPayload["stage"],
    iteration: count(progress.iteration),
    evidence: count(progress.evidence),
    ...(progress.stage === "stopped"
      ? { stop_reason: stop(progress.stop_reason) }
      : {}),
  };
}
