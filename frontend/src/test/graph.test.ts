import { expect, it } from "vitest";
import {
  autoLayout,
  compatible,
  explainConnection,
  fixedSiblings,
  toGraph,
  toWorkflow,
  withFixedNodes,
  type GraphNode,
} from "../workflow/graph";
import type { NodeMetadata, PortInfo, Workflow } from "../api/workflows";

const port = (dtype: string, overrides: Partial<PortInfo> = {}): PortInfo => ({
  dtype,
  optional: false,
  collect: false,
  requires_tag: null,
  ...overrides,
});

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
    inputs: { value: port("string") },
    outputs: { again: "string" },
    config_schema: {},
  },
];
// 端口基数只在需要时单独造一套目录，避免影响固定节点补回的用例。
const cardinalityCatalog: NodeMetadata[] = [
  ...catalog,
  {
    type: "collector",
    title: "Collector",
    category: "transform",
    inputs: { values: port("string", { optional: true, collect: true }) },
    outputs: { again: "string" },
    config_schema: {},
  },
  {
    type: "splitter",
    title: "Splitter",
    category: "transform",
    inputs: { value: port("string") },
    outputs: { values: "list<string>" },
    config_schema: {},
  },
];
// 后端 /nodes 现在标注 fixed/canonical：输入与输出类别常驻，缺失时按 canonical 补回。
const fixedCatalog: NodeMetadata[] = catalog.map((item) => ({
  ...item,
  fixed: true,
  canonical: true,
}));
const nodes: GraphNode[] = [
  {
    id: "a",
    position: { x: 0, y: 0 },
    data: { nodeType: "source", config: {} },
  },
  { id: "b", position: { x: 1, y: 1 }, data: { nodeType: "sink", config: {} } },
];
const cardinalityNodes: GraphNode[] = [
  ...nodes,
  {
    id: "split",
    position: { x: 2, y: 2 },
    data: { nodeType: "splitter", config: {} },
  },
  {
    id: "join",
    position: { x: 3, y: 3 },
    data: { nodeType: "collector", config: {} },
  },
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
it("lets a collect port gather several edges while a plain port takes one", () => {
  const intoPlain = {
    source: "a",
    sourceHandle: "value",
    target: "b",
    targetHandle: "value",
  };
  expect(
    compatible(intoPlain, cardinalityNodes, cardinalityCatalog, []),
  ).toBe(true);
  expect(
    compatible(intoPlain, cardinalityNodes, cardinalityCatalog, [
      { ...intoPlain, id: "a-b" },
    ]),
  ).toBe(false);
  // 普通端口也不接受 list<T>：形状变化必须由显式节点完成。
  expect(
    compatible(
      { source: "split", sourceHandle: "values", target: "b", targetHandle: "value" },
      cardinalityNodes,
      cardinalityCatalog,
      [],
    ),
  ).toBe(false);

  const first = {
    source: "a",
    sourceHandle: "value",
    target: "join",
    targetHandle: "values",
  };
  // collect 端口既收多个生产者，也直接吞下 list<T>，这就是“分解后多路检索”的连线。
  expect(
    compatible(
      { source: "split", sourceHandle: "values", target: "join", targetHandle: "values" },
      cardinalityNodes,
      cardinalityCatalog,
      [{ ...first, id: "a-join" }],
    ),
  ).toBe(true);
});
it("names the exact reason a connection is refused", () => {
  const into = (source: string, sourceHandle: string, target: string, targetHandle: string) =>
    explainConnection({ source, sourceHandle, target, targetHandle }, cardinalityNodes,
      cardinalityCatalog, []);

  expect(into("a", "value", "b", "value")).toBeNull();
  expect(into("a", "nope", "b", "value")).toBe("上游没有输出端口 nope");
  expect(into("a", "value", "b", "nope")).toBe("下游没有输入端口 nope");
  expect(into("split", "values", "b", "value"))
    .toBe("端口类型不匹配：list<string> → string");
  expect(into("a", "value", "a", "value")).toBe("节点不能连到自己");
  expect(
    explainConnection(
      { source: "a", sourceHandle: "value", target: "b", targetHandle: "value" },
      cardinalityNodes,
      cardinalityCatalog,
      [{ id: "a-b", source: "a", target: "b", sourceHandle: "value", targetHandle: "value" }],
    ),
  ).toBe("value 只接受一条连线；要汇聚多个上游请选带 ⇉ 的端口（如 retrieval.rrf.sources）");
  // join 的祖先 split 还有一个空闲输入口，连回去就是环。
  expect(
    explainConnection(
      { source: "join", sourceHandle: "again", target: "split", targetHandle: "value" },
      cardinalityNodes,
      cardinalityCatalog,
      [
        { id: "s-j", source: "split", target: "join", sourceHandle: "values", targetHandle: "values" },
      ],
    ),
  ).toBe("这样连会形成环路");
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

it("restores only the fixed category that has no instance", () => {
  const withBoth = toGraph(
    {
      id: "demo",
      name: "Demo",
      version: 1,
      status: "published",
      nodes: [
        { id: "a", type: "source", position: { x: 120, y: 40 }, config: {} },
        { id: "b", type: "sink", position: { x: 400, y: 40 }, config: {} },
      ],
      edges: [],
    },
    fixedCatalog,
  ).nodes;
  expect(withBoth.map((node) => node.deletable)).toEqual([false, false]);
  expect(withFixedNodes(withBoth, fixedCatalog)).toEqual({
    nodes: withBoth,
    added: [],
  });

  const onlyInput = [withBoth[0]];
  const restored = withFixedNodes(onlyInput, fixedCatalog);
  expect(restored.added.map((item) => item.type)).toEqual(["sink"]);
  expect(restored.nodes).toHaveLength(2);
  expect(restored.nodes[1].position.x).toBeGreaterThan(120);
  expect(restored.nodes[1].deletable).toBe(false);
});

it("counts same-category nodes so the last fixed one stays locked", () => {
  const [first, second, extra] = toGraph(
    {
      id: "demo",
      name: "Demo",
      version: 1,
      status: "published",
      nodes: [
        { id: "a", type: "source", position: { x: 0, y: 0 }, config: {} },
        { id: "b", type: "sink", position: { x: 0, y: 0 }, config: {} },
        { id: "c", type: "sink", position: { x: 0, y: 0 }, config: {} },
      ],
      edges: [],
    },
    fixedCatalog,
  ).nodes;
  const all = [first, second, extra];
  expect(fixedSiblings(first, all)).toBe(0);
  expect(fixedSiblings(second, all)).toBe(1);
});
