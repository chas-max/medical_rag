import { expect, it } from "vitest";
import { isAnswerWorkflow, selectAnswerWorkflows } from "../workflow/selection";
import type { Workflow } from "../api/workflows";

const node = (id: string, type: string): Workflow["nodes"][number] => ({
  id,
  type,
  position: { x: 0, y: 0 },
  config: {},
});

const edge = (
  source: string,
  source_port: string,
  target: string,
  target_port: string,
): Workflow["edges"][number] => ({
  id: `${source}-${target}`,
  source,
  source_port,
  target,
  target_port,
});

const answerWorkflow: Workflow = {
  id: "answer",
  name: "回答",
  version: 1,
  status: "published",
  nodes: [
    node("query", "query.input"),
    node("retrieval", "retrieval.dense"),
    node("gate", "evidence.gate"),
    node("llm", "generation.llm"),
    node("citation", "citation.validate"),
    node("output", "answer.output"),
  ],
  edges: [
    edge("query", "question", "retrieval", "question"),
    edge("retrieval", "evidence", "gate", "evidence"),
    edge("gate", "evidence", "llm", "evidence"),
    edge("query", "question", "llm", "question"),
    edge("llm", "draft", "citation", "draft"),
    edge("gate", "evidence", "citation", "evidence"),
    edge("citation", "answer", "output", "answer"),
  ],
};

const searchWorkflow: Workflow = {
  ...answerWorkflow,
  id: "search",
  nodes: [
    node("query", "query.input"),
    node("retrieval", "retrieval.dense"),
    node("output", "search.output"),
  ],
  edges: [edge("query", "question", "retrieval", "question")],
};

it("recognizes a complete answer workflow", () => {
  expect(isAnswerWorkflow(answerWorkflow)).toBe(true);
});

it("rejects a search-only workflow", () => {
  expect(isAnswerWorkflow(searchWorkflow)).toBe(false);
});

it("filters the list without mutating it", () => {
  const workflows = [searchWorkflow, answerWorkflow];
  expect(selectAnswerWorkflows(workflows).map((item) => item.id)).toEqual([
    "answer",
  ]);
  expect(workflows).toHaveLength(2);
});
