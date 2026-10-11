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
  const output = Object.entries(meta?.outputs || {});  return (
    <div
      className={`flow-node category-${meta?.category || "unknown"} ${selected ? "selected" : ""} ${data.traceStatus || ""} ${meta?.fixed ? "fixed" : ""}`}
    >
      <div className="flow-node-heading">
        <span className="flow-icon">{icons[meta?.category || ""] || "◇"}</span>
        <div>
          <strong>{meta?.title || data.nodeType}</strong>
          <small>{data.nodeType}</small>
        </div>
        {meta?.fixed ? (
          <span className="flow-node-lock" title="固定节点：常驻画布，不可单独删除">
            固定
          </span>
        ) : (
          <span className="flow-node-menu">⋮</span>
        )}
      </div>
      <div className="flow-node-ports">
        <div>
          {input.map(([name, port]) => (
            <div
              className={`port input-port${port.optional ? " port-optional" : ""}${
                port.collect ? " port-collect" : ""
              }`}
              key={name}
            >
              <Handle type="target" position={Position.Left} id={name} />
              <span>{name}</span>
              <small title={port.collect ? "可接多条连线，值会收成列表" : port.optional ? "可以不连线" : "必须连线"}>
                {port.dtype}
              </small>
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
