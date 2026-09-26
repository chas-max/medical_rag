from pathlib import Path

import pytest

from medical_rag.workflow.definition import WorkflowDefinition
from medical_rag.workflow.repository import WorkflowRepository


def definition(version: int) -> WorkflowDefinition:
    return WorkflowDefinition(id="medical-answer", name="医疗问答", version=version,
                              status="published", nodes=[], edges=[])


def test_versions_are_immutable_and_latest_is_selected(tmp_path: Path):
    repo = WorkflowRepository(tmp_path)
    repo.save(definition(1))
    repo.save(definition(2))
    assert repo.get("medical-answer", 1).version == 1
    assert repo.get("medical-answer").version == 2
    assert [item.version for item in repo.list()] == [2]
    with pytest.raises(ValueError):
        repo.save(definition(1))


def test_repository_rejects_traversal_and_invalid_json(tmp_path: Path):
    repo = WorkflowRepository(tmp_path)
    with pytest.raises(ValueError):
        repo.save(definition(1).model_copy(update={"id": "../escape"}))
    (tmp_path / "broken.v1.json").write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        repo.get("broken", 1)
