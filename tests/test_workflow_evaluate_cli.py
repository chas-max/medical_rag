"""Workflow CLI validation is read-only and precedes paid dependency creation."""

import json

import pytest
from test_evaluation import Judge
from test_workflow_evaluation import setup

from medical_rag.evaluate import main
from medical_rag.workflow.repository import WorkflowRepository


def paths(tmp_path):
    dataset, output = tmp_path / "cases.jsonl", tmp_path / "report.json"
    dataset.write_text('{"question":"问题","relevant_chunk_ids":["web-2"]}', encoding="utf-8")
    return ["--dataset", str(dataset), "--output", str(output)], output


@pytest.mark.parametrize("options", [["--workflow-version", "1"],
                                     ["--workflow-id", "medical-answer-agentic", "--workflow-version", "0"],
                                     ["--workflow-id", "missing"], ["--workflow-id", "../bad"],
                                     ["--workflow-id", "local-retrieval-correction"],
                                     ["--workflow-id", "medical-answer", "--workflow-version", "1"]])
def test_rejects_workflow_arguments_before_dependencies(tmp_path, options):
    prefix, _ = paths(tmp_path)
    calls = []
    assert main(prefix + options, factory=lambda *args: calls.append(args)) == 2
    assert calls == []


def test_cli_opt_in_runs_graph_and_records_fixed_version(tmp_path):
    prefix, output = paths(tmp_path)
    service, _ = setup(tmp_path / "workflows")
    assert main(prefix + ["--workflow-id", "medical-answer-agentic", "--workflow-version", "1"],
                factory=lambda *_: (service, Judge())) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["config"]["workflow_id"] == "medical-answer-agentic"
    assert report["config"]["workflow_version"] == 1
    assert report["cases"][0]["agent_model_calls"] == 3 and service.runs


def test_cli_resolves_once_before_factory_can_publish_new_version(tmp_path, monkeypatch):
    prefix, output = paths(tmp_path)
    service, _ = setup(tmp_path / "workflows")
    import medical_rag.evaluate as cli

    monkeypatch.setattr(cli, "DEFAULT_WORKFLOWS", tmp_path / "workflows", raising=False)

    def factory(*_):
        graph = service.repository.get("medical-answer-agentic", 1).model_copy(deep=True, update={"version": 2})
        next(node for node in graph.nodes if node.type == "evidence.gate").config["threshold"] = 1
        service.repository.save(graph)
        return service, Judge()

    # Populate CLI's local repository without any external dependencies.
    WorkflowRepository(tmp_path / "workflows").save(service.repository.get("medical-answer-agentic", 1))
    WorkflowRepository(tmp_path / "workflows").save(service.repository.get("local-retrieval-correction", 1))
    assert main(prefix + ["--workflow-id", "medical-answer-agentic"], factory=factory) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["config"]["workflow_version"] == 1


def test_invalid_published_graph_rejected_before_dependencies(tmp_path, monkeypatch):
    prefix, _ = paths(tmp_path)
    service, _ = setup(tmp_path / "workflows")
    import medical_rag.evaluate as cli

    graph = service.repository.get("medical-answer-agentic", 1).model_copy(deep=True, update={"edges": []})
    WorkflowRepository(tmp_path / "workflows").save(graph)
    monkeypatch.setattr(cli, "DEFAULT_WORKFLOWS", tmp_path / "workflows")
    calls = []
    assert main(prefix + ["--workflow-id", graph.id], factory=lambda *args: calls.append(args)) == 2
    assert not calls


def test_blank_provider_rejected_before_workflow_dependencies(tmp_path):
    prefix, _ = paths(tmp_path)
    calls = []
    assert main(prefix + ["--workflow-id", "medical-answer-agentic", "--answer-provider", " "],
                factory=lambda *args: calls.append(args)) == 2
    assert not calls
