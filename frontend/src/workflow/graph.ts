import type { Connection, Edge, Node } from "@xyflow/react";
import type {
  NodeMetadata,
  Workflow,
  WorkflowEdge,
  WorkflowNode,
} from "../api/workflows";

export type GraphNode = Node<{
  nodeType: string;
  metadata?: NodeMetadata;
  config: WorkflowNode["config"];
  traceStatus?: string;
}>;

export const toGraph = (workflow: Workflow, catalog: NodeMetadata[]) => ({
  nodes: workflow.nodes.map((node) => ({
    id: node.id,
    type: "workflow",
    position: node.position,
    data: {
      nodeType: node.type,
      metadata: catalog.find((item) => item.type === node.type),
      config: node.config,
    },
  })) as GraphNode[],
  edges: workflow.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    sourceHandle: edge.source_port,
    targetHandle: edge.target_port,
    type: "smoothstep",
    animated: false,
  })) as Edge[],
});

export function toWorkflow(
  original: Workflow,
  nodes: GraphNode[],
  edges: Edge[],
  status: Workflow["status"] = "draft",
): Workflow {
  return {
    ...original,
    version: original.version + 1,
    status,
    nodes: nodes.map((node) => ({
      id: node.id,
      type: node.data.nodeType,
      position: node.position,
      config: node.data.config,
    })),
    edges: edges.map(
      (edge) =>
        ({
          id: edge.id,
          source: edge.source,
          source_port: edge.sourceHandle || "",
          target: edge.target,
          target_port: edge.targetHandle || "",
        }) satisfies WorkflowEdge,
    ),
  };
}

export function compatible(
  connection: Connection,
  nodes: GraphNode[],
  catalog: NodeMetadata[],
  edges: Edge[],
): boolean {
  if (
    !connection.source ||
    !connection.target ||
    !connection.sourceHandle ||
    !connection.targetHandle ||
    connection.source === connection.target
  )
    return false;
  const source = nodes.find((node) => node.id === connection.source);
  const target = nodes.find((node) => node.id === connection.target);
  const from = catalog.find((item) => item.type === source?.data.nodeType)
    ?.outputs[connection.sourceHandle];
  const to = catalog.find((item) => item.type === target?.data.nodeType)
    ?.inputs[connection.targetHandle];
  if (
    !from ||
    from !== to ||
    edges.some(
      (edge) =>
        edge.target === connection.target &&
        edge.targetHandle === connection.targetHandle,
    )
  )
    return false;
  // Reject a connection that would immediately introduce a directed cycle.
  const seen = new Set<string>();
  const visit = (id: string): boolean => {
    if (id === connection.source) return true;
    if (seen.has(id)) return false;
    seen.add(id);
    return edges
      .filter((edge) => edge.source === id)
      .some((edge) => visit(edge.target));
  };
  return !visit(connection.target);
}

export function autoLayout(nodes: GraphNode[], edges: Edge[]): GraphNode[] {
  const depth = new Map(nodes.map((node) => [node.id, 0]));
  const incoming = new Map(
    nodes.map((node) => [
      node.id,
      edges.filter((edge) => edge.target === node.id).length,
    ]),
  );
  const queue = nodes
    .filter((node) => incoming.get(node.id) === 0)
    .map((node) => node.id);
  while (queue.length) {
    const id = queue.shift()!;
    for (const edge of edges.filter((item) => item.source === id)) {
      depth.set(
        edge.target,
        Math.max(depth.get(edge.target) || 0, (depth.get(id) || 0) + 1),
      );
      incoming.set(edge.target, (incoming.get(edge.target) || 0) - 1);
      if (incoming.get(edge.target) === 0) queue.push(edge.target);
    }
  }
  const rows = new Map<number, number>();
  return nodes.map((node) => {
    const column = depth.get(node.id) || 0;
    const row = rows.get(column) || 0;
    rows.set(column, row + 1);
    return { ...node, position: { x: column * 255 + 60, y: row * 210 + 80 } };
  });
}
