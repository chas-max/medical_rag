from dataclasses import dataclass, field

from medical_rag.workflow.contracts import ExecutionContext, NodeResult, PortSpec
from medical_rag.workflow.definition import (
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)
from medical_rag.workflow.registry import NodeRegistry
from medical_rag.workflow.validator import validate_workflow


@dataclass
class StubNode:
    type: str
    title: str
    category: str
    inputs: dict[str, str | PortSpec] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    config_schema: dict[str, object] = field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    shared_producers: tuple[tuple[str, str], ...] = ()

    async def execute(
        self,
        inputs: dict[str, object],
        config: dict[str, object],
        context: ExecutionContext,
    ) -> NodeResult:
        return NodeResult(outputs={}, status="success")


def make_definition(
    nodes: list[dict[str, object]], edges: list[dict[str, str]]
) -> WorkflowDefinition:
    return WorkflowDefinition(
        id="demo",
        name="Demo",
        version=1,
        status="draft",
        nodes=[WorkflowNode.model_validate(node) for node in nodes],
        edges=[WorkflowEdge.model_validate(edge) for edge in edges],
    )


def make_registry(*nodes: StubNode) -> NodeRegistry:
    registry = NodeRegistry()
    for node in nodes:
        registry.register(node)
    return registry


def test_validator_returns_no_issues_for_a_connected_workflow() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "answer", "type": "answer.output"},
        ],
        [
            {
                "id": "question-edge",
                "source": "query",
                "source_port": "question",
                "target": "answer",
                "target_port": "question",
            }
        ],
    )
    registry = make_registry(
        StubNode(
            "query.input",
            "Query",
            "input",
            outputs={"question": "string"},
        ),
        StubNode(
            "answer.output",
            "Answer",
            "output",
            inputs={"question": "string"},
        ),
    )

    assert validate_workflow(definition, registry) == []


def test_validator_reports_cycle_and_missing_required_port() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "left", "type": "worker.left"},
            {"id": "right", "type": "worker.right"},
        ],
        [
            {
                "id": "query-left",
                "source": "query",
                "source_port": "question",
                "target": "left",
                "target_port": "question",
            },
            {
                "id": "left-right",
                "source": "left",
                "source_port": "value",
                "target": "right",
                "target_port": "left_value",
            },
            {
                "id": "right-left",
                "source": "right",
                "source_port": "value",
                "target": "left",
                "target_port": "context",
            },
        ],
    )
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode(
            "worker.left",
            "Left",
            "transform",
            inputs={"question": "string", "context": "string", "required": "string"},
            outputs={"value": "string"},
        ),
        StubNode(
            "worker.right",
            "Right",
            "output",
            inputs={"left_value": "string"},
            outputs={"value": "string"},
        ),
    )

    issues = validate_workflow(definition, registry)

    assert {issue.code for issue in issues} == {"CYCLE", "MISSING_INPUT"}
    missing = next(issue for issue in issues if issue.code == "MISSING_INPUT")
    assert (missing.node_id, missing.edge_id) == ("left", None)
    assert missing.message


def test_validator_reports_unknown_types_ports_and_incompatible_port_types() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "worker", "type": "worker.transform"},
            {"id": "answer", "type": "answer.output"},
            {"id": "missing", "type": "not.registered"},
        ],
        [
            {
                "id": "bad-source-port",
                "source": "query",
                "source_port": "unknown",
                "target": "worker",
                "target_port": "question",
            },
            {
                "id": "bad-target-port",
                "source": "worker",
                "source_port": "value",
                "target": "answer",
                "target_port": "unknown",
            },
            {
                "id": "wrong-type",
                "source": "worker",
                "source_port": "value",
                "target": "answer",
                "target_port": "evidence",
            },
        ],
    )
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode(
            "worker.transform",
            "Worker",
            "transform",
            inputs={"question": "string"},
            outputs={"value": "string"},
        ),
        StubNode(
            "answer.output",
            "Answer",
            "output",
            inputs={"evidence": "evidence"},
        ),
    )

    issues = validate_workflow(definition, registry)
    by_edge = {(issue.code, issue.edge_id) for issue in issues}

    assert ("UNKNOWN_NODE_TYPE", None) in by_edge
    assert ("UNKNOWN_SOURCE_PORT", "bad-source-port") in by_edge
    assert ("UNKNOWN_TARGET_PORT", "bad-target-port") in by_edge
    assert ("PORT_TYPE_MISMATCH", "wrong-type") in by_edge


def test_validator_reports_missing_edge_endpoints() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "answer", "type": "answer.output"},
        ],
        [
            {
                "id": "missing-source",
                "source": "ghost",
                "source_port": "question",
                "target": "answer",
                "target_port": "question",
            },
            {
                "id": "missing-target",
                "source": "query",
                "source_port": "question",
                "target": "ghost",
                "target_port": "question",
            },
        ],
    )
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode("answer.output", "Answer", "output", inputs={"question": "string"}),
    )

    issues = validate_workflow(definition, registry)

    assert {issue.code for issue in issues} >= {
        "MISSING_SOURCE_NODE",
        "MISSING_TARGET_NODE",
    }
    assert all(
        issue.edge_id
        for issue in issues
        if issue.code in {"MISSING_SOURCE_NODE", "MISSING_TARGET_NODE"}
    )


def test_validator_reports_missing_entry_exit_and_unreachable_nodes() -> None:
    definition = make_definition(
        [
            {"id": "first", "type": "worker.transform"},
            {"id": "second", "type": "worker.transform"},
        ],
        [],
    )
    registry = make_registry(
        StubNode("worker.transform", "Worker", "transform", outputs={"value": "string"})
    )

    issues = validate_workflow(definition, registry)

    assert {issue.code for issue in issues} >= {
        "MISSING_INPUT_NODE",
        "MISSING_OUTPUT_NODE",
        "UNREACHABLE_NODE",
    }
    assert {issue.node_id for issue in issues if issue.code == "UNREACHABLE_NODE"} == {
        "first",
        "second",
    }


def test_validator_reports_duplicate_node_and_edge_ids_stably() -> None:
    definition = make_definition(
        [
            {"id": "query", "type": "query.input"},
            {"id": "answer", "type": "answer.output"},
        ],
        [
            {
                "id": "same-edge",
                "source": "query",
                "source_port": "question",
                "target": "answer",
                "target_port": "question",
            }
        ],
    )
    definition.nodes.append(WorkflowNode(id="query", type="query.input"))
    definition.edges.append(
        WorkflowEdge(
            id="same-edge",
            source="query",
            source_port="question",
            target="answer",
            target_port="question",
        )
    )
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode("answer.output", "Answer", "output", inputs={"question": "string"}),
    )

    first = validate_workflow(definition, registry)
    second = validate_workflow(definition, registry)

    assert [
        (issue.code, issue.node_id, issue.edge_id, issue.message) for issue in first
    ] == [(issue.code, issue.node_id, issue.edge_id, issue.message) for issue in second]
    assert {issue.code for issue in first} >= {"DUPLICATE_NODE_ID", "DUPLICATE_EDGE_ID"}


ANSWER_TYPES = {
    "query": "query.input",
    "dense": "retrieval.dense",
    "gate": "evidence.gate",
    "generation": "generation.llm",
    "citation": "citation.validate",
    "output": "answer.output",
    "rewrite": "question.rewrite",
    "decompose": "question.decompose",
    "multi": "retrieval.multi",
}


def nodes_named(*names: str) -> list[dict[str, object]]:
    return [{"id": name, "type": ANSWER_TYPES[name]} for name in names]


def gated_registry() -> NodeRegistry:
    """A retrieval chain plus a guarded generation step, mirroring the answer nodes."""
    return make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode("retrieval.dense", "Dense", "retrieval", inputs={"question": "string"},
                 outputs={"evidence": "evidence_list"}),
        StubNode("evidence.gate", "Gate", "evidence",
                 inputs={"evidence": "evidence_list"}, outputs={"evidence": "evidence_list"},
                 tags=frozenset({"evidence.gated"})),
        StubNode("generation.llm", "Generation", "generation",
                 inputs={"question": "string",
                         "evidence": PortSpec("evidence", "evidence_list",
                                              requires_tag="evidence.gated")},
                 outputs={"draft": "string", "evidence": "evidence_list"}),
        StubNode("citation.validate", "Citation", "citation",
                 inputs={"draft": "string", "evidence": "evidence_list"},
                 outputs={"answer": "answer"}, tags=frozenset({"answer.cited"}),
                 shared_producers=(("draft", "evidence"),)),
        StubNode("answer.output", "Answer", "output",
                 inputs={"answer": PortSpec("answer", "answer", requires_tag="answer.cited")}),
    )


def chain(*connections: tuple[str, str, str, str]) -> list[dict[str, str]]:
    return [
        {"id": f"{source}-{target}-{index}", "source": source, "source_port": source_port,
         "target": target, "target_port": target_port}
        for index, (source, source_port, target, target_port) in enumerate(connections)
    ]


SAFE_CHAIN = chain(
    ("query", "question", "generation", "question"),
    ("query", "question", "dense", "question"),
    ("dense", "evidence", "gate", "evidence"),
    ("gate", "evidence", "generation", "evidence"),
    ("generation", "draft", "citation", "draft"),
    ("generation", "evidence", "citation", "evidence"),
    ("citation", "answer", "output", "answer"),
)


def test_declared_guards_are_satisfied_by_the_guarded_chain() -> None:
    definition = make_definition(
        nodes_named("query", "dense", "gate", "generation", "citation", "output"), SAFE_CHAIN)

    assert validate_workflow(definition, gated_registry()) == []


def test_tag_requirement_rejects_evidence_that_bypasses_the_guard() -> None:
    """重排/fusion 换成别的节点都行，但证据绕过门控就必须拒绝。"""
    definition = make_definition(
        nodes_named("query", "dense", "gate", "generation", "citation", "output"),
        chain(
            ("query", "question", "generation", "question"),
            ("query", "question", "dense", "question"),
            ("dense", "evidence", "gate", "evidence"),
            # 门控存在于图里，却不在通往 generation.evidence 的路径上。
            ("dense", "evidence", "generation", "evidence"),
            ("generation", "draft", "citation", "draft"),
            ("generation", "evidence", "citation", "evidence"),
            ("citation", "answer", "output", "answer"),
        ),
    )

    issues = validate_workflow(definition, gated_registry())

    assert {issue.code for issue in issues} == {"UNSAFE_ANSWER_FLOW"}
    assert next(issue for issue in issues).node_id == "generation"


def test_optional_ports_may_stay_unconnected() -> None:
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode("question.rewrite", "Rewrite", "question",
                 inputs={"question": "string",
                         "route": PortSpec("route", "string", optional=True)},
                 outputs={"value": "string"}),
        StubNode("answer.output", "Answer", "output", inputs={"value": "string"}),
    )
    definition = make_definition(nodes_named("query", "rewrite", "output"),
                                 chain(("query", "question", "rewrite", "question"),
                                       ("rewrite", "value", "output", "value")))

    assert validate_workflow(definition, registry) == []


def test_plain_ports_take_one_edge_while_collect_ports_gather() -> None:
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode("retrieval.one", "One", "retrieval", inputs={"question": "string"},
                 outputs={"value": "string"}),
        StubNode("retrieval.two", "Two", "retrieval", inputs={"question": "string"},
                 outputs={"value": "string"}),
        StubNode("join.plain", "Plain", "output", inputs={"values": "string"}),
        StubNode("join.collect", "Collect", "output",
                 inputs={"values": PortSpec("values", "string", collect=True)}),
    )
    fan_in = chain(("query", "question", "one", "question"),
                   ("query", "question", "two", "question"))
    shared = chain(("one", "value", "join", "values"), ("two", "value", "join", "values"))

    plain = validate_workflow(
        make_definition([{"id": "query", "type": "query.input"},
                         {"id": "one", "type": "retrieval.one"},
                         {"id": "two", "type": "retrieval.two"},
                         {"id": "join", "type": "join.plain"}], fan_in + shared),
        registry,
    )
    collect = validate_workflow(
        make_definition([{"id": "query", "type": "query.input"},
                         {"id": "one", "type": "retrieval.one"},
                         {"id": "two", "type": "retrieval.two"},
                         {"id": "join", "type": "join.collect"}], fan_in + shared),
        registry,
    )

    assert {issue.code for issue in plain} == {"MULTI_INPUT_EDGE"}
    assert collect == []


def test_collect_port_accepts_a_list_producer_of_the_same_item_type() -> None:
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode("question.decompose", "Decompose", "question", inputs={"question": "string"},
                 outputs={"values": "list<string>"}),
        StubNode("retrieval.multi", "Multi", "retrieval",
                 inputs={"queries": PortSpec("queries", "string", collect=True)},
                 outputs={"evidence": "evidence_list"}),
        StubNode("answer.output", "Answer", "output", inputs={"evidence": "evidence_list"}),
    )
    definition = make_definition(nodes_named("query", "decompose", "multi", "output"),
                                 chain(("query", "question", "decompose", "question"),
                                       ("decompose", "values", "multi", "queries"),
                                       ("multi", "evidence", "output", "evidence")))

    assert validate_workflow(definition, registry) == []


def test_draft_and_evidence_must_come_from_the_same_producer() -> None:
    """引用校验只能校验生成节点真正见过的那份证据，否则联网补充的网页块会绕过逐字校验。"""
    split = make_definition(
        nodes_named("query", "dense", "gate", "generation", "citation", "output"),
        chain(
            ("query", "question", "generation", "question"),
            ("query", "question", "dense", "question"),
            ("dense", "evidence", "gate", "evidence"),
            ("gate", "evidence", "generation", "evidence"),
            ("generation", "draft", "citation", "draft"),
            ("gate", "evidence", "citation", "evidence"),
            ("citation", "answer", "output", "answer"),
        ),
    )

    issues = validate_workflow(split, gated_registry())

    assert [issue.code for issue in issues] == ["PROVENANCE_MISMATCH"]
    assert issues[0].node_id == "citation"


def test_activation_must_reference_a_declared_input_port() -> None:
    registry = make_registry(
        StubNode("query.input", "Query", "input", outputs={"question": "string"}),
        StubNode("question.rewrite", "Rewrite", "question", inputs={"question": "string"},
                 outputs={"value": "string"}),
        StubNode("answer.output", "Answer", "output", inputs={"value": "string"}),
    )
    definition = make_definition(
        [{"id": "query", "type": "query.input"},
         {"id": "rewrite", "type": "question.rewrite",
          "activation": {"port": "route", "equals": "rewrite"}},
         {"id": "output", "type": "answer.output"}],
        chain(("query", "question", "rewrite", "question"),
              ("rewrite", "value", "output", "value")),
    )

    issues = validate_workflow(definition, registry)

    assert [(issue.code, issue.node_id) for issue in issues] == [("INVALID_ACTIVATION", "rewrite")]
