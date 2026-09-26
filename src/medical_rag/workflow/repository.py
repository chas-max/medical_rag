"""Immutable, versioned JSON workflow definitions."""

import json
import os
import re
import tempfile
from pathlib import Path

from .definition import WorkflowDefinition


class WorkflowRepository:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _name(workflow_id: str) -> str:
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", workflow_id):
            raise ValueError("无效的工作流 ID")
        return workflow_id

    def _path(self, workflow_id: str, version: int) -> Path:
        if version < 1:
            raise ValueError("版本必须为正整数")
        return self.root / f"{self._name(workflow_id)}.v{version}.json"

    def get(self, workflow_id: str, version: int | None = None) -> WorkflowDefinition:
        self._name(workflow_id)
        if version is None:
            versions = [int(match.group(1)) for path in self.root.glob(f"{workflow_id}.v*.json")
                        if (match := re.fullmatch(rf"{re.escape(workflow_id)}\.v(\d+)\.json", path.name))]
            if not versions:
                raise KeyError(workflow_id)
            version = max(versions)
        path = self._path(workflow_id, version)
        if not path.is_file():
            raise KeyError((workflow_id, version))
        try:
            definition = WorkflowDefinition.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ValueError("工作流定义 JSON 无效") from exc
        if definition.id != workflow_id or definition.version != version:
            raise ValueError("工作流定义与文件名不匹配")
        return definition

    def list(self) -> list[WorkflowDefinition]:
        ids = {match.group(1) for path in self.root.glob("*.json")
               if (match := re.fullmatch(r"([a-zA-Z][a-zA-Z0-9_-]{0,63})\.v\d+\.json", path.name))}
        return [self.get(workflow_id) for workflow_id in sorted(ids)]

    def save(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        path = self._path(definition.id, definition.version)
        self.root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise ValueError("工作流版本已存在")
        data = definition.model_dump_json(indent=2).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".workflow-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            # A hard link publishes the fully written file atomically, without replacing versions.
            os.link(temporary, path)
            index = self.root / ".index.json"
            versions = {item.id: item.version for item in self.list()}
            index_descriptor, index_name = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(index_descriptor, "w", encoding="utf-8") as index_output:
                    json.dump(versions, index_output, ensure_ascii=False)
                os.replace(index_name, index)
            finally:
                Path(index_name).unlink(missing_ok=True)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return definition
