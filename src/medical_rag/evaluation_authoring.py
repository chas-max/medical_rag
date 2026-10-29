"""Materialize manually authored drafts against immutable source excerpts."""

from __future__ import annotations

from pathlib import Path

from .evaluation_anchors import make_anchor_id, normalize_source_text
from .evaluation_dataset import validate_evaluation_row

_COLUMNS = (
    "id", "question_type", "difficulty", "answerability", "question",
    "reference_answer", "key_points", "source_quote",
)


def _source_sections(source_path: Path) -> list[tuple[str | int, str]]:
    if source_path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return [(index, page.extract_text() or "")
                for index, page in enumerate(PdfReader(str(source_path)).pages, 1)]
    if source_path.suffix.lower() == ".md":
        sections: list[tuple[str | int, str]] = []
        heading = "文档说明"
        lines: list[str] = []
        for line in source_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if lines:
                    sections.append((heading, "\n".join(lines)))
                heading, lines = line[3:].strip(), []
            else:
                lines.append(line)
        sections.append((heading, "\n".join(lines)))
        return sections
    raise ValueError("只支持 PDF 或 UTF-8 Markdown 来源")


def _comparable(text: str) -> str:
    return "".join(normalize_source_text(text).split())


def materialize_draft(draft_path: Path, source_id: str, source_path: Path,
                      category: str) -> list[dict]:
    """Build rich rows, rejecting excerpts not found on a single source page/section."""
    lines = draft_path.read_text(encoding="utf-8").splitlines()
    if not lines or tuple(lines[0].split("|")) != _COLUMNS:
        raise ValueError("草稿表头无效")
    sections = _source_sections(source_path)
    rows = []
    for line_number, line in enumerate(lines[1:], 2):
        cells = line.split("|")
        if len(cells) != len(_COLUMNS):
            raise ValueError(f"第 {line_number} 行列数无效")
        draft = dict(zip(_COLUMNS, cells))
        references = []
        for quote in draft["source_quote"].split(" § "):
            quote = quote.strip()
            selected_locator: str | int | None = None
            if quote.startswith("@page:") or quote.startswith("@heading:"):
                marker, separator, quote = quote.partition("::")
                if not separator:
                    raise ValueError(f"题目 {draft['id']} 的来源定位格式无效")
                kind, value = marker[1:].split(":", 1)
                selected_locator = int(value) if kind == "page" else value
                quote = quote.strip()
            matches = [(locator, text) for locator, text in sections
                       if (selected_locator is None or locator == selected_locator)
                       and _comparable(quote) in _comparable(text)]
            if not quote or not matches:
                raise ValueError(f"题目 {draft['id']} 的摘录不在来源中")
            if len(matches) > 1:
                raise ValueError(f"题目 {draft['id']} 的摘录跨多个来源位置，需人工定位")
            locator = matches[0][0]
            heading_path = [locator] if isinstance(locator, str) else [f"page-{locator}"]
            references.append({
                "source_id": source_id,
                "locator": {"heading": locator} if isinstance(locator, str) else {"page": locator},
                "quote": quote,
                "anchor_id": make_anchor_id(source_id, heading_path, quote),
            })
        row = {
            "id": draft["id"], "question": draft["question"],
            "reference_answer": draft["reference_answer"],
            "key_points": [point.strip() for point in draft["key_points"].split("；")],
            "answerability": draft["answerability"], "category": category,
            "question_type": draft["question_type"], "difficulty": draft["difficulty"],
            "source_refs": references, "relevant_chunk_ids": [],
        }
        rows.append(validate_evaluation_row(row, line_number))
    return rows
