import type { Workflow } from "../api/workflows";

function hasPath(
  adjacency: Map<string, string[]>,
  start: string,
  target: string,
): boolean {
  const pending = [start];
  const visited = new Set<string>();
  while (pending.length) {
    const current = pending.shift();
    if (!current || visited.has(current)) continue;
    if (current === target) return true;
    visited.add(current);
    pending.push(...(adjacency.get(current) || []));
  }
  return false;
}

function nodeIds(workflow: Workflow, predicate: (type: string) => boolean) {
  return workflow.nodes.filter((node) => predicate(node.type)).map((node) => node.id);
}

export function isAnswerWorkflow(workflow: Workflow): boolean {
  const adjacency = new Map<string, string[]>();
  for (const edge of workflow.edges) {
    adjacency.set(edge.source, [...(adjacency.get(edge.source) || []), edge.target]);
  }

  const inputs = nodeIds(workflow, (type) => type === "query.input");
  const retrievals = nodeIds(workflow, (type) => type.startsWith("retrieval.") && type !== "retrieval.rerank");
  const gates = nodeIds(workflow, (type) => type === "evidence.gate");
  const generations = nodeIds(workflow, (type) => type === "generation.llm");
  const citations = nodeIds(workflow, (type) => type === "citation.validate");
  const outputs = nodeIds(workflow, (type) => type === "answer.output");

  return inputs.some((input) =>
    retrievals.some((retrieval) =>
      gates.some((gate) =>
        generations.some((generation) =>
          citations.some((citation) =>
            outputs.some((output) =>
              hasPath(adjacency, input, retrieval) &&
              hasPath(adjacency, retrieval, gate) &&
              hasPath(adjacency, gate, generation) &&
              hasPath(adjacency, generation, citation) &&
              hasPath(adjacency, citation, output),
            ),
          ),
        ),
      ),
    ),
  );
}

export function selectAnswerWorkflows(workflows: Workflow[]): Workflow[] {
  return workflows.filter(isAnswerWorkflow);
}
