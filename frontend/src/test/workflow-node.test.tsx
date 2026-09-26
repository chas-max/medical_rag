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
            inputs: { question: "string" },
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
});
