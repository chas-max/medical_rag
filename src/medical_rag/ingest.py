"""Parse text documents and route them to structure-aware chunkers."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

from .types import Chunk


@dataclass(frozen=True)
class Paragraph:
    text: str
    page: int | None = None
    section: str = ""


@dataclass(frozen=True)
class ParsedDocument:
    filename: str
    sha256: str
    paragraphs: list[Paragraph]


def _paragraphs(text: str, page: int | None = None, section: str = "") -> list[Paragraph]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return [Paragraph(re.sub(r"[ \t]+", " ", part.strip()), page, section)
            for part in re.split(r"\n\s*\n", text) if part.strip()]


def parse_document(filename: str, data: bytes) -> ParsedDocument:
    """Parse UTF-8 Markdown/TXT or a PDF with extractable text."""
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not data:
        raise ValueError("文件没有内容")
    if suffix == "pdf":
        try:
            reader = PdfReader(BytesIO(data))
            paragraphs = [p for page_no, page in enumerate(reader.pages, start=1)
                          for p in _paragraphs(page.extract_text() or "", page_no)]
        except Exception as exc:
            raise ValueError("无法解析 PDF 文件") from exc
        if not paragraphs:
            raise ValueError("PDF 没有可提取的文字；扫描版需要 OCR")
    elif suffix in {"md", "txt"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("文本文件必须使用 UTF-8 编码") from exc
        if suffix == "md":
            sections: list[str] = []
            current: list[str] = []
            paragraphs = []

            def flush() -> None:
                paragraphs.extend(_paragraphs("\n".join(current), section=" / ".join(sections)))
                current.clear()

            for line in text.splitlines():
                heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
                if heading:
                    flush()
                    level = len(heading.group(1))
                    sections = sections[: level - 1] + [heading.group(2).strip()]
                else:
                    current.append(line)
            flush()
        else:
            paragraphs = _paragraphs(text)
        if not paragraphs:
            raise ValueError("文档没有可用文字")
    else:
        raise ValueError("不支持该文件格式")
    return ParsedDocument(filename, hashlib.sha256(data).hexdigest(), paragraphs)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norms = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norms if norms else 1.0


def _recursive(text: str, limit: int, overlap: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            cut = max((text.rfind(marker, start + max(1, limit // 2), end)
                       for marker in ("\n", "。", "！", "？", ".", "!", "?")), default=-1)
            if cut >= 0:
                end = cut + 1
        part = text[start:end].strip()
        if part:
            chunks.append(part)
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def chunk_document(
    doc: ParsedDocument, embedder: object, max_chars: int = 700,
    overlap: int = 80, semantic_threshold: float = 0.65,
) -> tuple[str, str, list[Chunk]]:
    """Select a structure, semantic, or recursive strategy and preserve provenance."""
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("分块大小与重叠参数无效")
    is_markdown = doc.filename.lower().endswith(".md")
    paragraphs = doc.paragraphs
    embeddings = None
    if not is_markdown and len(paragraphs) >= 3 and all(len(p.text) >= 8 for p in paragraphs):
        embeddings = embedder.encode([p.text for p in paragraphs])
    boundaries = set()
    if embeddings is not None:
        boundaries = {i for i in range(1, len(paragraphs))
                      if _cosine(embeddings[i - 1], embeddings[i]) < semantic_threshold}
    if is_markdown:
        strategy, reason = "markdown", "检测到 Markdown 标题层级"
    elif boundaries:
        strategy, reason = "semantic", "相邻段落的主题相似度低于阈值"
    else:
        strategy, reason = "recursive", "无明显标题或主题边界，按长度递归切分"

    groups: list[list[Paragraph]] = []
    for i, paragraph in enumerate(paragraphs):
        changed = groups and (paragraph.page != groups[-1][-1].page
                              or (is_markdown and paragraph.section != groups[-1][-1].section)
                              or (strategy == "semantic" and i in boundaries))
        if changed or not groups:
            groups.append([])
        groups[-1].append(paragraph)
    document_id = doc.sha256
    chunks = []
    for group in groups:
        content = "\n\n".join(p.text for p in group)
        for part in _recursive(content, max_chars, overlap):
            ordinal = len(chunks)
            chunk_id = hashlib.sha256(f"{document_id}:{ordinal}:{part}".encode()).hexdigest()
            chunks.append(Chunk(chunk_id, document_id, doc.filename, part, doc.filename,
                                group[0].section, group[0].page, ordinal))
    return strategy, reason, chunks
