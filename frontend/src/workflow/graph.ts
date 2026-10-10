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

const categoryOf = (node: GraphNode) => node.data.metadata?.category || "";

/** 同类别的其它节点数量：固定节点只有在这归零时才是不可删的。 */
export const fixedSiblings = (node: GraphNode, nodes: GraphNode[]) =>
  nodes.filter((item) => item.id !== node.id && categoryOf(item) === categoryOf(node)).length;

export function createGraphNode(
  meta: NodeMetadata,
  position: { x: number; y: number },
): GraphNode {
  return {
    id: `${meta.type.replaceAll(".", "-")}-${crypto.randomUUID().slice(0, 6)}`,
    type: "workflow",
    position,
    deletable: !meta.fixed,
    data: {
      nodeType: meta.type,
      metadata: meta,
      config: Object.fromEntries(
        Object.entries(meta.config_schema)
          .filter(([, schema]) => schema.default !== undefined)
          .map(([key, schema]) => [key, schema.default!]),
      ),
    },
  };
}

export const toGraph = (workflow: Workflow, catalog: NodeMetadata[]) => ({
  nodes: workflow.nodes.map((node) => {
    const metadata = catalog.find((item) => item.type === node.type);
    return {
      id: node.id,
      type: "workflow",
      position: node.position,
      deletable: !metadata?.fixed,
      data: {
        nodeType: node.type,
        metadata,
        config: node.config,
      },
    };
  }) as GraphNode[],
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

/**
 * 输入/输出类节点是固定节点：图里缺整类时按后端标注的规范类型补回，
 * 保证画布始终有一条从固定入口走到固定出口的完整路径。
 */
export function withFixedNodes(
  nodes: GraphNode[],
  catalog: NodeMetadata[],
): { nodes: GraphNode[]; added: NodeMetadata[] } {
  const added: NodeMetadata[] = [];
  const columns = nodes.map((node) => node.position.x);
  const left = columns.length ? Math.min(...columns) - 255 : 60;
  const right = columns.length ? Math.max(...columns) + 255 : 570;
  const next = [...nodes];
  for (const category of new Set(
    catalog.filter((item) => item.fixed).map((item) => item.category),
  )) {
    if (next.some((node) => categoryOf(node) === category)) continue;
    const meta =
      catalog.find((item) => item.canonical && item.category === category) ||
      catalog.find((item) => item.fixed && item.category === category);
    if (!meta) continue;
    next.push(createGraphNode(meta, { x: category === "input" ? left : right, y: 80 }));
    added.push(meta);
  }
  return { nodes: next, added };
}

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
