import { render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { expect, it, vi } from "vitest";
import WorkflowNode from "../components/workflow/WorkflowNode";
import type { GraphNode } from "../workflow/graph";

vi.mock("@xyflow/react", () => ({
  Handle: ({ id, type }: { id: string; type: string }) => (
    <span data-testid={`${type}-${id}`} />
  ),
  Position: { Left: "left", Right: "right" },
}));

it("renders schema-defined ports and execution status", () => {
  render(
    <WorkflowNode
      {...({
        id: "dense",
        selected: true,
        data: {
          nodeType: "retrieval.dense",
          metadata: {
            type: "retrieval.dense",
            title: "向量检索",
            category: "retrieval",
            inputs: {
              question: {
                dtype: "string",
                optional: false,
                collect: false,
                requires_tag: null,
              },
            },
            outputs: { evidence: "evidence_list" },
            config_schema: {},
          },
          config: {},
          traceStatus: "success",
        },
      } as unknown as NodeProps<GraphNode>)}
    />,
  );
  expect(screen.getByText("向量检索")).toBeInTheDocument();
  expect(screen.getByTestId("target-question")).toBeInTheDocument();
  expect(screen.getByTestId("source-evidence")).toBeInTheDocument();
  expect(screen.getByText("success")).toBeInTheDocument();
  expect(screen.queryByText("固定")).not.toBeInTheDocument();
});

it("marks fixed nodes as resident instead of showing the overflow menu", () => {
  const { container } = render(
    <WorkflowNode
      {...({
        id: "query",
        selected: false,
        data: {
          nodeType: "query.input",
          metadata: {
            type: "query.input",
            title: "问题输入",
            category: "input",
            inputs: {},
            outputs: { question: "string" },
            config_schema: {},
            fixed: true,
          },
          config: {},
        },
      } as unknown as NodeProps<GraphNode>)}
    />,
  );

  expect(container.querySelector(".flow-node")?.className).toContain("fixed");
  expect(container.querySelector(".flow-node-lock")?.textContent).toBe("固定");
  expect(container.querySelector(".flow-node-menu")).toBeNull();
});
