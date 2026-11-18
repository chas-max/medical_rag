"""Release validation, chunk remapping, and judge export utilities.

These functions deliberately keep file I/O deterministic and avoid any model or
vector-store dependencies.  Source references are the durable truth; chunk IDs
are derived artifacts produced by :func:`remap_release`.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .evaluation_anchors import make_anchor_id, match_anchor, normalize_source_text, source_sha256
from .evaluation_authoring import _comparable, _source_sections
from .evaluation_dataset import load_evaluation_dataset, validate_quotas


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"无法读取 JSON 文件: {path.name}") from exc


def _registry_entries(registry: Any) -> list[dict[str, Any]]:
    entries = registry.get("sources", registry) if isinstance(registry, dict) else registry
    if isinstance(entries, dict):
        entries = [{"source_id": key, **(value if isinstance(value, dict) else {})}
                   for key, value in entries.items()]
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ValueError("source registry 格式无效")
    return entries


def _source_ids_and_hashes(registry: Any) -> tuple[set[str], dict[str, str | None]]:
    entries = _registry_entries(registry)
    source_ids: set[str] = set()
    hashes: dict[str, str | None] = {}
    for entry in entries:
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source registry 缺少 source_id")
        source_id = source_id.strip()
        if source_id in source_ids:
            raise ValueError(f"source_id 重复: {source_id}")
        source_ids.add(source_id)
        digest = entry.get("sha256", entry.get("file_sha256"))
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            raise ValueError(f"source {source_id} 的 sha256 无效")
        hashes[source_id] = digest
    return source_ids, hashes


def _check_registry_hashes(
    registry: Any,
    manifest: dict[str, Any] | None = None,
    *,
    registry_path: Path | None = None,
) -> tuple[set[str], dict[str, str | None]]:
    source_ids, hashes = _source_ids_and_hashes(registry)
    required = {}
    if isinstance(manifest, dict):
        raw = manifest.get("source_hashes", manifest.get("required_source_hashes", {}))
        if isinstance(raw, dict):
            required = raw
    for source_id, expected in required.items():
        if source_id not in source_ids:
            raise ValueError(f"source registry 缺少来源: {source_id}")
        actual = hashes.get(source_id)
        if not actual or actual.lower() != str(expected).lower():
            raise ValueError(f"source {source_id} 的 sha256 不匹配")
    entries = _registry_entries(registry)
    for entry in entries:
        digest = entry.get("sha256", entry.get("file_sha256"))
        source_id = entry.get("source_id", "")
        if not digest:
            raise ValueError(f"source {source_id} 缺少 sha256")
        path_value = entry.get("path", entry.get("filename", entry.get("file")))
        if not path_value:
            raise ValueError(f"source {source_id} 缺少文件路径")
        path = Path(path_value)
        if not path.is_absolute():
            # Resolve relative to registry location when available.
            if registry_path:
                path = registry_path.parent / path
        if not path.exists() or not path.is_file():
            raise ValueError(f"source {source_id} 文件不存在")
        if source_sha256(path).lower() != digest.lower():
            raise ValueError(f"source {entry['source_id']} 的 sha256 不匹配")
    return source_ids, hashes


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def validate_release(dataset_path: Path, manifest_path: Path, registry_path: Path) -> dict[str, Any]:
    """Validate a complete rich release and return a safe summary."""
    manifest = _read_json(manifest_path)
    registry = _read_json(registry_path)
    if not isinstance(manifest, dict):
        raise ValueError("manifest 格式无效")
    for field, path, label in (
        ("dataset_sha256", dataset_path, "题库哈希"),
        ("source_registry_sha256", registry_path, "source registry 哈希"),
        ("rubric_sha256", manifest_path.parent / "rubric.md", "rubric 哈希"),
    ):
        expected_digest = manifest.get(field)
        if expected_digest is None:
            continue
        if (not isinstance(expected_digest, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_digest)
                or not path.is_file()
                or source_sha256(path).lower() != expected_digest.lower()):
            raise ValueError(f"{label} 不匹配")
    source_ids, hashes = _check_registry_hashes(registry, manifest, registry_path=registry_path)
    source_sections = {}
    for entry in _registry_entries(registry):
        path = Path(entry.get("path", entry.get("filename", entry.get("file"))))
        if not path.is_absolute():
            path = registry_path.parent / path
        if path.suffix.lower() in {".pdf", ".md"}:
            source_sections[entry["source_id"]] = dict(_source_sections(path))
    expected = manifest.get("question_count", manifest.get("expected_count"))
    cases = load_evaluation_dataset(dataset_path, expected_count=expected)
    validate_quotas(cases, manifest)
    seen_questions: dict[str, str] = {}
    referenced: set[str] = set()
    for case in cases:
        key = normalize_source_text(case["question"]).casefold()
        if key in seen_questions:
            raise ValueError(f"问题重复: {case['id']} 与 {seen_questions[key]}")
        seen_questions[key] = case["id"]
        for ref in case["source_refs"]:
            source_id = ref["source_id"]
            if source_id not in source_ids:
                raise ValueError(f"题目 {case['id']} 引用了未知来源: {source_id}")
            if source_id in source_sections:
                locator = ref["locator"]
                location = locator.get("page", locator.get("heading"))
                text = source_sections[source_id].get(location)
                if text is None or _comparable(ref["quote"]) not in _comparable(text):
                    raise ValueError(f"题目 {case['id']} 的来源摘录与定位不匹配")
                heading_path = [location] if isinstance(location, str) else [f"page-{location}"]
                if ref["anchor_id"] != make_anchor_id(source_id, heading_path, ref["quote"]):
                    raise ValueError(f"题目 {case['id']} 的稳定锚点不匹配")
            referenced.add(source_id)
    quota_counts = {}
    for dimension in ("category", "question_type", "difficulty", "answerability"):
        quota_counts[dimension] = {}
        for case in cases:
            value = case[dimension]
            quota_counts[dimension][value] = quota_counts[dimension].get(value, 0) + 1
    return {
        "question_count": len(cases),
        "source_ids": sorted(referenced),
        "registered_source_count": len(source_ids),
        "source_hash_count": sum(value is not None for value in hashes.values()),
        "quota_counts": quota_counts,
        "dataset_path": str(dataset_path),
    }


def _load_chunks(path: Path) -> list[dict[str, Any]]:
    value = _read_json(path)
    chunks = value.get("chunks") if isinstance(value, dict) else value
    if not isinstance(chunks, list):
        raise ValueError("chunk inventory 必须是 JSON 数组")
    normalized = []
    seen: set[str] = set()
    for index, chunk in enumerate(chunks, 1):
        if not isinstance(chunk, dict):
            raise ValueError(f"第 {index} 个 chunk 格式无效")
        chunk_id = chunk.get("id", chunk.get("chunk_id"))
        source_id = chunk.get("source_id")
        text = chunk.get("text", chunk.get("content"))
        if not all(isinstance(item, str) and item.strip() for item in (chunk_id, source_id, text)):
            raise ValueError(f"第 {index} 个 chunk 缺少 id/source_id/text")
        if ("page" not in chunk or "section" not in chunk
                or (chunk["page"] is not None and
                    (isinstance(chunk["page"], bool) or not isinstance(chunk["page"], int)
                     or chunk["page"] < 0))
                or not isinstance(chunk["section"], str)):
            raise ValueError(f"第 {index} 个 chunk 缺少 page/section")
        digest = chunk.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError(f"第 {index} 个 chunk 缺少有效 sha256")
        if chunk_id in seen:
            raise ValueError(f"chunk_id 重复: {chunk_id}")
        seen.add(chunk_id)
        normalized.append({**chunk, "chunk_id": chunk_id, "id": chunk_id, "text": text,
                           "page": chunk["page"] or None})
    return normalized


def remap_release(dataset_path: Path, chunks_path: Path, registry_path: Path, output_path: Path) -> dict[str, Any]:
    """Resolve stable source anchors against the current deterministic inventory."""
    sidecar = output_path.with_suffix(output_path.suffix + ".mapping.json")
    if output_path.resolve() in {path.resolve() for path in (dataset_path, chunks_path, registry_path)}:
        raise ValueError("输出路径不能覆盖输入文件")
    if output_path.exists() or sidecar.exists():
        raise ValueError("输出文件已存在，请选择新路径")
    registry = _read_json(registry_path)
    source_ids, source_hashes = _check_registry_hashes(registry, registry_path=registry_path)
    cases = load_evaluation_dataset(dataset_path)
    chunks = _load_chunks(chunks_path)
    chunk_sources = {chunk["source_id"] for chunk in chunks}
    if not chunk_sources <= source_ids:
        unknown = sorted(chunk_sources - source_ids)
        raise ValueError(f"chunk inventory 包含未知来源: {', '.join(unknown)}")
    for chunk in chunks:
        if chunk["sha256"].lower() != source_hashes[chunk["source_id"]].lower():
            raise ValueError(f"chunk {chunk['chunk_id']} 的 sha256 与注册来源不符")
    runtime_rows: list[dict[str, Any]] = []
    mapping_report: list[dict[str, Any]] = []
    try:
        for case in cases:
            row = dict(case)
            resolved: list[str] = []
            grades: dict[str, int] = {}
            context_ids: list[str] = []
            for ref in case["source_refs"]:
                if ref.get("source_id") not in source_ids:
                    raise ValueError(f"题目 {case['id']} 引用了未知来源: {ref.get('source_id')}")
                if case.get("answerability") == "insufficient_evidence":
                    if ref.get("relevance") == 0:
                        result = match_anchor(ref, chunks)
                        for chunk_id in result.get("chunk_ids", [result.get("chunk_id")]):
                            if chunk_id not in context_ids:
                                context_ids.append(chunk_id)
                            mapping_report.append({
                                "question_id": case["id"], "anchor": ref.get("anchor_id"),
                                "chunk_id": chunk_id, "method": "context_only",
                                "score": result["score"], "warnings": ["insufficient_evidence"],
                            })
                    else:
                        mapping_report.append({"question_id": case["id"], "anchor": ref.get("anchor_id"),
                                               "chunk_id": None, "method": "skipped", "score": None,
                                               "warnings": ["insufficient_evidence"]})
                    continue
                grade = int(ref.get("relevance", 2))
                result = match_anchor(ref, chunks)
                for chunk_id in result.get("chunk_ids", [result.get("chunk_id")]):
                    if chunk_id not in resolved:
                        resolved.append(chunk_id)
                    grades[chunk_id] = max(grades.get(chunk_id, 0), grade)
                    mapping_report.append({"question_id": case["id"], "anchor": ref.get("anchor_id"),
                                           "chunk_id": chunk_id, "method": result["method"],
                                           "score": result["score"], "warnings": []})
            row["relevant_chunk_ids"] = resolved
            if grades:
                row["chunk_grades"] = grades
            if context_ids:
                row["context_chunk_ids"] = context_ids
            runtime_rows.append(row)
    except (KeyError, TypeError, ValueError) as exc:
        # Do not leave a successful-looking runtime copy behind on failure.
        raise ValueError(str(exc)) from exc
    payload = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                      for row in runtime_rows)
    try:
        _atomic_write(sidecar, json.dumps(mapping_report, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(output_path, payload)
    except Exception:
        output_path.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise
    return {"question_count": len(runtime_rows), "mapping_count": len(mapping_report),
            "output_path": str(output_path), "mapping_report": str(sidecar)}


def _safe_score(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("score")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("score 无效")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0 <= numeric <= 1:
        raise ValueError("score 超出范围")
    return numeric


def export_judge_cases(report_path: Path, dataset_path: Path, output_path: Path) -> int:
    """Export a provider-neutral, secret-free JSONL judge payload."""
    report = _read_json(report_path)
    if not isinstance(report, dict) or not isinstance(report.get("cases"), list):
        raise ValueError("evaluation report 格式无效")
    dataset = {case["id"]: case for case in load_evaluation_dataset(dataset_path)}
    rubric_version = report.get("rubric_version")
    if rubric_version is None and isinstance(report.get("config"), dict):
        rubric_version = report["config"].get("rubric_version")
    rubric_version = str(rubric_version or "medical-health-v1")
    exported: list[dict[str, Any]] = []
    for item in report["cases"]:
        if not isinstance(item, dict) or item.get("id") not in dataset:
            raise ValueError("report 中存在未知题目 ID")
        source = dataset[item["id"]]
        answer = item.get("answer")
        if isinstance(answer, dict):
            candidate = answer.get("answer")
        else:
            candidate = answer
        evidence = item.get("evidence", [])
        excerpts = [entry.get("text", "") for entry in evidence
                     if isinstance(entry, dict) and isinstance(entry.get("text", ""), str)]
        scores = {name: _safe_score(item.get(name)) for name in
                  ("faithfulness", "context_relevance", "answer_relevance")}
        exported.append({
            "id": item["id"],
            "question": source["question"],
            "reference_answer": source["reference_answer"],
            "key_points": source["key_points"],
            "source_excerpts": [ref["quote"] for ref in source["source_refs"]],
            "evidence_excerpts": excerpts,
            "candidate_answer": candidate if isinstance(candidate, str) else None,
            "answerability": source["answerability"],
            "question_type": source["question_type"],
            "difficulty": source["difficulty"],
            "rubric_version": rubric_version,
            "scores": scores,
        })
    _atomic_write(output_path, "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                                       for item in exported))
    return len(exported)
