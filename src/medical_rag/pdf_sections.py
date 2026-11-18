"""PDF section tracking, page-artifact cleaning, and index metadata formatting."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

MAX_SECTION_HEADER_CHARS = 72

_ORPHAN_PAGE_NUMBER = re.compile(
    r"^(?:[-—·|]\s*)?\d{1,4}(?:\s*/\s*\d{1,4})?(?:\s*[-—·|])?$|"
    r"^第\s*\d{1,4}\s*页(?:\s*/\s*共\s*\d+\s*页)?$"
)
_JOURNAL_FOOTER = re.compile(
    r"^·?\s*\d{1,4}\s*·?\s*[\u4e00-\u9fffA-Za-z（）()]+杂志.*(?:Vol\.|期)"
)
_DOTTED_LEADER = re.compile(r"(?:\.{4,}|…{2,}|·{4,})\s*\d*\s*$")
_TOC_TITLE = re.compile(r"^目\s*录$")
_REFERENCES_TITLE = re.compile(r"^(?:主要)?参考文献(?:\s*[:：]?\s*)?$|^References\s*$", re.IGNORECASE)
_CITATION_ENTRY = re.compile(r"^\[\d+\]\s+[^\n]*(?:\d{4}|et\s+al|杂志|Journal|Vol\.|中华|doi)", re.IGNORECASE)
_ANY_BRACKET_CITATION = re.compile(r"^\[\d+\]\s+\S+")
_COVER_TITLE_ONLY = re.compile(
    r"^(?:[\u4e00-\u9fffA-Za-z0-9（）()\s]{2,42}(?:指南|规范|纲要|共识|指导原则|手册|问答))"
    r"(?:\s*[（(][^）)]{2,24}[）)])?\s*$"
)
_NUMBERED_TOC_LINE = re.compile(r"^(?:附\s*[件录]\s*[:：]?\s*)?\d{1,2}[.、．]\s*[^\n。；]{2,32}$")

_MAJOR_HEADINGS = (
    re.compile(r"^第[一二三四五六七八九十百\d]+[章部分篇]\s*[\u4e00-\u9fffA-Za-z0-9（）()《》、\-\s]{1,36}$"),
    re.compile(r"^[一二三四五六七八九十]+、\s*[\u4e00-\u9fffA-Za-z0-9（）()《》、\-\s]{2,34}$"),
    re.compile(r"^附录\s*[A-Za-z0-9一二三]+(?:[\s:：、.]+[^\n]{1,34})?$"),
)
_MINOR_HEADINGS = (
    re.compile(r"^[（(][一二三四五六七八九十]+[)）]\s*[^\n]{2,34}$"),
    re.compile(r"^\d{1,2}(?:\.\d{1,2}){1,2}\s+[\u4e00-\u9fffA-Za-z][^\n]{2,34}$"),
    re.compile(r"^(?:表|续表|图)\s*\d+(?:\.\d+)?(?:\s+[^\n]{2,32})?$"),
)
_PROSE_TERMINATORS = ("。", "；", "：", ":", "，", ",", "！", "？", "!", "?", "”")
_CLINICAL_NOISE = re.compile(
    r"(?:推荐|建议|应该|患者应|禁用|慎用|不良反应|发生率|mmol|mmhg|mg|μg|ug|kg|kcal|岁以上)"
    r"|[。；！？]",
    re.IGNORECASE,
)


def clean_pdf_page_lines(raw_text: str) -> list[str]:
    """Strip orphan page-number lines only at top/bottom page boundaries.

    只清理每页首尾两行内孤立的页码与期刊页脚，绝不误删正文或表格中间的孤立数字。
    """
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    non_empty_indices = [idx for idx, line in enumerate(lines) if line.strip()]
    if not non_empty_indices:
        return []
    edge_indices = set(non_empty_indices[:2] + non_empty_indices[-2:])
    cleaned: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if idx in edge_indices and (
            _ORPHAN_PAGE_NUMBER.match(stripped) or _JOURNAL_FOOTER.match(stripped)
        ):
            continue
        cleaned.append(line)
    return cleaned


def _collapse_cjk_gaps(text: str) -> str:
    return re.sub(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])", "", text).strip()


def _is_toc_page(lines: list[str]) -> bool:
    non_empty = [_collapse_cjk_gaps(line) for line in lines if line.strip()]
    if any(_TOC_TITLE.match(line) for line in non_empty[:5]):
        return True
    dotted = sum(1 for line in lines if _DOTTED_LEADER.search(line.strip()))
    return dotted >= 4


def _is_committee_frontmatter(lines: list[str], page_no: int) -> bool:
    if page_no > 6:
        return False
    head = "\n".join(_collapse_cjk_gaps(line) for line in lines[:12] if line.strip())
    markers = ("编写委员会", "专家委员会", "起草单位", "编写专家", "主审专家")
    if not any(marker in head for marker in markers):
        return False
    return head.count("。") <= 2


def _match_heading(line: str) -> tuple[int, str] | None:
    """Detect structural Chinese guideline headings without capturing prose lines."""
    collapsed = _collapse_cjk_gaps(line)
    if not 2 <= len(collapsed) <= 44:
        return None
    if collapsed.endswith(_PROSE_TERMINATORS):
        return None
    if _CLINICAL_NOISE.search(collapsed):
        return None
    if _MAJOR_HEADINGS[0].match(collapsed) or _MAJOR_HEADINGS[1].match(collapsed) or _MAJOR_HEADINGS[2].match(collapsed):
        return (1, collapsed)
    if _MINOR_HEADINGS[0].match(collapsed) or _MINOR_HEADINGS[1].match(collapsed) or _MINOR_HEADINGS[2].match(collapsed):
        return (2, collapsed)
    return None


def _compose_section(major: str, minor: str) -> str:
    if major and minor:
        section = f"{major} / {minor}"
    else:
        section = major or minor
    if len(section) <= MAX_SECTION_HEADER_CHARS:
        return section
    if minor and len(minor) <= MAX_SECTION_HEADER_CHARS:
        return minor
    return section[-MAX_SECTION_HEADER_CHARS:].lstrip(" /")


def _emit_block(
    lines: list[str],
    page_no: int,
    section: str,
    paragraph_builder: Callable[..., list],
    output: list,
) -> None:
    text = "\n".join(lines).strip()
    if not text:
        return
    output.extend(paragraph_builder(text, page_no, section))


def extract_pdf_paragraphs(reader: object, paragraph_builder: Callable[..., list]) -> list:
    """Extract PDF paragraphs with causal section tracking and clean page edges."""
    paragraphs: list = []
    major = ""
    minor = ""
    in_references = False

    for page_no, page in enumerate(getattr(reader, "pages", ()), start=1):
        lines = clean_pdf_page_lines(page.extract_text() or "")
        if not any(line.strip() for line in lines):
            continue
        if _is_toc_page(lines):
            _emit_block(lines, page_no, "目录", paragraph_builder, paragraphs)
            continue
        if _is_committee_frontmatter(lines, page_no):
            _emit_block(lines, page_no, "编写委员会", paragraph_builder, paragraphs)
            continue

        buffer: list[str] = []
        has_body = False
        first_non_empty = [_collapse_cjk_gaps(line) for line in lines[:5] if line.strip()]
        if in_references and not any(_ANY_BRACKET_CITATION.match(line) for line in first_non_empty):
            in_references = False
        active_section = "参考文献" if in_references else _compose_section(major, minor)
        for idx, line in enumerate(lines):
            collapsed = _collapse_cjk_gaps(line)
            if _REFERENCES_TITLE.match(collapsed):
                trailing_citations = sum(
                    1 for rem in lines[idx + 1:] if _ANY_BRACKET_CITATION.match(rem.strip())
                )
                if trailing_citations >= 2:
                    _emit_block(buffer, page_no, active_section, paragraph_builder, paragraphs)
                    buffer = [line]
                    has_body = False
                    in_references = True
                    active_section = "参考文献"
                    continue
            heading = _match_heading(line)
            if heading is not None:
                level, title = heading
                if in_references:
                    in_references = False
                new_major = title if level == 1 else major
                new_minor = "" if level == 1 else title
                next_section = _compose_section(new_major, new_minor)
                if next_section != active_section and buffer and has_body:
                    _emit_block(buffer, page_no, active_section, paragraph_builder, paragraphs)
                    buffer = []
                    has_body = False
                major, minor = new_major, new_minor
                active_section = next_section
                buffer.append(line)
                continue
            if line.strip():
                has_body = True
            buffer.append(line)
        _emit_block(buffer, page_no, active_section, paragraph_builder, paragraphs)
    return paragraphs


def format_chunk_for_index(filename: str, section: str, text: str) -> str:
    """Prepend compact source document title and section to text for embedding/rerank.

    只用于向量化与交叉编码打分输入，绝不修改存储或 BM25 使用的 chunk.text 原文。
    """
    raw_name = (filename or "").strip()
    stem = Path(raw_name).stem if "." in raw_name else raw_name
    stem = stem or raw_name
    sec = (section or "").strip()
    if len(sec) > MAX_SECTION_HEADER_CHARS:
        tail = sec.rsplit(" / ", 1)[-1]
        sec = tail if len(tail) <= MAX_SECTION_HEADER_CHARS else sec[-MAX_SECTION_HEADER_CHARS:]
    if stem and sec:
        return f"《{stem}》 ({sec})\n{text}"
    if stem:
        return f"《{stem}》\n{text}"
    if sec:
        return f"({sec})\n{text}"
    return text


def is_administrative_boilerplate(text: str, section: str = "") -> bool:
    """Identify cover pages, TOCs, rosters, or citation-list chunks unsuitable as primary evidence."""
    sec = (section or "").strip()
    if sec in {"目录", "编写委员会", "参考文献"} or sec.startswith(("目录 /", "编写委员会 /", "参考文献 /")):
        return True
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    collapsed = _collapse_cjk_gaps(" ".join(lines))
    if not sec and len(collapsed) <= 48 and "。" not in collapsed and _COVER_TITLE_ONLY.match(collapsed):
        return True
    if sum(1 for line in lines if _DOTTED_LEADER.search(line)) >= 3:
        return True
    if sum(1 for line in lines if _CITATION_ENTRY.match(line)) >= 3 and sec != "参考文献":
        return True
    if sum(1 for line in lines if _NUMBERED_TOC_LINE.match(_collapse_cjk_gaps(line))) >= 5 and collapsed.count("。") <= 1:
        return True
    if sec == "" and any(_TOC_TITLE.match(_collapse_cjk_gaps(line)) for line in lines[:3]):
        return True
    return False
