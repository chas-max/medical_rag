import {
  safeProgress,
  safeRunTrace,
  type SafeNodeTrace,
} from "../workflow/runTrace";

function NodeStatus({ node }: { node: SafeNodeTrace }) {
  return (
    <span>
      {node.node_id} · {node.status} · {node.duration_ms} ms
      {node.error ? ` · ${node.error.code}` : ""}
    </span>
  );
}

export function RunProgress({ progress }: { progress: unknown }) {
  const safe = safeProgress(progress);
  if (!safe) return null;
  const stages = {
    assessing: "评估证据",
    planning: "规划补充查询",
    retrieving: "补充检索",
    stopped: "修正结束",
  };
  return (
    <div className="agent-progress" role="status">
      {safe.node_id} · 第 {safe.iteration} 轮 · {stages[safe.stage]} ·{" "}
      {safe.evidence} 条证据{safe.stop_reason ? ` · ${safe.stop_reason}` : ""}
    </div>
  );
}

export default function RunTrace({
  trace,
  runId,
}: {
  trace?: unknown;
  runId?: string;
}) {
  const nodes = safeRunTrace(trace);
  if (!nodes.length) return null;
  return (
    <details className="run-trace">
      <summary>运行轨迹{runId ? ` · ${runId}` : ""}</summary>
      <ul>
        {nodes.map((node, index) => (
          <li key={`${node.node_id}-${index}`}>
            <NodeStatus node={node} />
            {node.agentic && (
              <details>
                <summary>
                  检索修正 · {node.agentic.stop_reason} · 检索{" "}
                  {node.agentic.retrieval_rounds} 轮 · 评估/规划尝试{" "}
                  {node.agentic.model_calls} 次
                </summary>
                {node.agentic.iterations.map((round, index) => (
                  <details key={index}>
                    <summary>
                      第 {round.iteration} 轮 · {round.duration_ms} ms
                    </summary>
                    <p>查询: {round.queries.join("、") || "无"}</p>
                    <p>证据 ID: {round.evidence_ids.join("、") || "无"}</p>
                    {round.assessment && (
                      <p>
                        评估:{" "}
                        {round.assessment.sufficient ? "证据充分" : "待补充"} ·
                        缺口: {round.assessment.missing.join("、") || "无"} ·
                        支持 ID:{" "}
                        {round.assessment.supporting_chunk_ids.join("、") ||
                          "无"}
                      </p>
                    )}
                    <ul>
                      {round.trace.map((child, index) => (
                        <li key={index}>
                          <NodeStatus node={child} />
                        </li>
                      ))}
                    </ul>
                  </details>
                ))}
              </details>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}
