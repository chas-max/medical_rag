import { expect, it } from "vitest";
import {
  autoLayout,
  compatible,
  toWorkflow,
  type GraphNode,
} from "../workflow/graph";
import type { NodeMetadata, Workflow } from "../api/workflows";

const catalog: NodeMetadata[] = [
  {
    type: "source",
    title: "Source",
    category: "input",
    inputs: {},
    outputs: { value: "string" },
    config_schema: {},
  },
  {
    type: "sink",
    title: "Sink",
    category: "output",
    inputs: { value: "string" },
    outputs: { again: "string" },
    config_schema: {},
  },
];
const nodes: GraphNode[] = [
  {
    id: "a",
    position: { x: 0, y: 0 },
    data: { nodeType: "source", config: {} },
  },
  { id: "b", position: { x: 1, y: 1 }, data: { nodeType: "sink", config: {} } },
];
it("serializes canvas positions and typed ports to a new version", () => {
  const original: Workflow = {
    id: "demo",
    name: "Demo",
    version: 2,
    status: "published",
    nodes: [],
    edges: [],
  };
  expect(
    toWorkflow(original, nodes, [
      {
        id: "a-b",
        source: "a",
        target: "b",
        sourceHandle: "value",
        targetHandle: "value",
      },
    ]).edges[0],
  ).toEqual({
    id: "a-b",
    source: "a",
    source_port: "value",
    target: "b",
    target_port: "value",
  });
  expect(toWorkflow(original, nodes, []).version).toBe(3);
});
it("rejects invalid ports, duplicate inputs and cycles", () => {
  const good = {
    source: "a",
    sourceHandle: "value",
    target: "b",
    targetHandle: "value",
  };
  expect(compatible(good, nodes, catalog, [])).toBe(true);
  expect(
    compatible({ ...good, sourceHandle: "missing" }, nodes, catalog, []),
  ).toBe(false);
  const edges = [{ ...good, id: "a-b" }];
  expect(compatible(good, nodes, catalog, edges)).toBe(false);
  expect(
    compatible(
      {
        source: "b",
        sourceHandle: "again",
        target: "a",
        targetHandle: "value",
      },
      nodes,
      catalog,
      edges,
    ),
  ).toBe(false);
});
it("lays out DAG columns while retaining node IDs and data", () => {
  const laidOut = autoLayout(nodes, [{ id: "a-b", source: "a", target: "b" }]);
  expect(laidOut[0].position.x).toBeLessThan(laidOut[1].position.x);
  expect(laidOut[1].data.nodeType).toBe("sink");
});
