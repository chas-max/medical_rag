import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { GraphNode } from "../../workflow/graph";

const icons: Record<string, string> = {
  input: "↳",
  retrieval: "⌕",
  fusion: "⊕",
  rerank: "☷",
  evidence: "✳",
  generation: "✦",
  citation: "⌁",
  output: "↗",
  document: "▤",
};

export default function WorkflowNode({ data, selected }: NodeProps<GraphNode>) {
  const meta = data.metadata;
  const input = Object.entries(meta?.inputs || {});
  const output = Object.entries(meta?.outputs || {});
  return (
    <div
      className={`flow-node category-${meta?.category || "unknown"} ${selected ? "selected" : ""} ${data.traceStatus || ""}`}
    >
      <div className="flow-node-heading">
        <span className="flow-icon">{icons[meta?.category || ""] || "◇"}</span>
        <div>
          <strong>{meta?.title || data.nodeType}</strong>
          <small>{data.nodeType}</small>
        </div>
        <span className="flow-node-menu">⋮</span>
      </div>
      <div className="flow-node-ports">
        <div>
          {input.map(([name, type]) => (
            <div className="port input-port" key={name}>
              <Handle type="target" position={Position.Left} id={name} />
              <span>{name}</span>
              <small>{type}</small>
            </div>
          ))}
        </div>
        <div>
          {output.map(([name, type]) => (
            <div className="port output-port" key={name}>
              <span>{name}</span>
              <small>{type}</small>
              <Handle type="source" position={Position.Right} id={name} />
            </div>
          ))}
        </div>
      </div>
      {data.traceStatus && (
        <div className={`node-run-status ${data.traceStatus}`}>
          {data.traceStatus}
        </div>
      )}
    </div>
  );
}
