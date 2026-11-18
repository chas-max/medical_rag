import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medical_rag.ingest import chunk_document, parse_document
from medical_rag.tokenization import MAX_CHUNK_TOKENS, TokenCounter


class TopicEmbedder:
    def encode(self, texts):
        return [[1.0, 0.0] if "血压" in text else [0.0, 1.0] for text in texts]


class CharTokenizer:
    """Deterministic stand-in for the BGE tokenizer (one token per character)."""

    def encode(self, text, add_special_tokens=False):
        return list(text)


COUNTER = TokenCounter(tokenizer=CharTokenizer())


def test_markdown_headings_are_preserved_with_chunks():
    parsed = parse_document("guide.md", "# 高血压\n\n血压持续升高。\n\n## 管理\n\n规律随访。".encode())
    strategy, reason, chunks = chunk_document(parsed, TopicEmbedder())
    assert strategy == "markdown"
    assert reason
    assert [chunk.section for chunk in chunks] == ["高血压", "高血压 / 管理"]
    assert all(chunk.filename == "guide.md" for chunk in chunks)


def test_topic_shift_routes_txt_to_semantic():
    text = "血压管理需要规律复诊。\n\n血压监测可记录变化。\n\n糖尿病管理关注血糖。"
    parsed = parse_document("guide.txt", text.encode())
    strategy, _, chunks = chunk_document(parsed, TopicEmbedder(), max_chars=80, overlap=10)
    assert strategy == "semantic"
    assert len(chunks) == 2
    assert "糖尿病" in chunks[-1].text


def test_short_txt_routes_to_recursive_without_losing_sentences():
    parsed = parse_document("short.txt", "第一句话。第二句话。第三句话。第四句话。".encode())
    strategy, _, chunks = chunk_document(parsed, TopicEmbedder(), max_chars=10, overlap=2)
    assert strategy == "recursive"
    assert all(len(chunk.text) <= 10 for chunk in chunks)
    assert all(word in "".join(chunk.text for chunk in chunks) for word in ("第一句", "第二句", "第三句", "第四句"))
    assert [chunk.id for chunk in chunks] == [chunk.id for chunk in chunk_document(parsed, TopicEmbedder(), max_chars=10, overlap=2)[2]]


def test_pdf_page_metadata_survives_chunking():
    pdf = fitz.open()
    for page_text in ("Blood pressure follow up.", "Diabetes monitoring."):
        page = pdf.new_page()
        page.insert_text((72, 72), page_text)
    parsed = parse_document("guide.pdf", pdf.tobytes())
    _, _, chunks = chunk_document(parsed, TopicEmbedder())
    assert {chunk.page for chunk in chunks} == {1, 2}
    assert "Blood pressure" in next(chunk.text for chunk in chunks if chunk.page == 1)


def test_scanned_pdf_without_text_is_rejected():
    pdf = fitz.open()
    pdf.new_page()
    with pytest.raises(ValueError, match="文字"):
        parse_document("scan.pdf", pdf.tobytes())


def test_overlong_markdown_heading_is_chunked_as_content_with_bounded_metadata():
    heading = "标题起点" + "医" * 400 + "标题终点"
    parsed = parse_document("long.md", f"# {heading}\n\n相关正文。".encode())
    strategy, _, chunks = chunk_document(parsed, TopicEmbedder(), max_chars=200, overlap=20)
    assert strategy == "markdown"
    assert len(chunks) > 2
    assert chunks[0].text.startswith("标题起点")
    assert any("标题终点" in chunk.text for chunk in chunks)
    assert all(len(chunk.section.encode("utf-8")) <= 1024 for chunk in chunks)


def test_default_chunking_is_token_bounded_and_keeps_parent_context():
    body = "\n\n".join(f"第{index}段：高血压患者应规律随访，监测血压变化。" for index in range(60))
    parsed = parse_document("long.txt", body.encode())
    _, _, chunks = chunk_document(
        parsed, TopicEmbedder(), max_tokens=120, overlap_tokens=18,
        parent_max_tokens=300, token_counter=COUNTER)

    assert len(chunks) >= 3
    assert all(0 < chunk.token_count <= 120 for chunk in chunks)
    assert all(chunk.parent_id and chunk.parent_text for chunk in chunks)
    # Every child must be readable inside the parent window handed to the generator.
    assert all(chunk.text in chunk.parent_text for chunk in chunks)
    parents = {chunk.parent_id: chunk.parent_text for chunk in chunks}
    assert len(parents) < len(chunks)
    assert all(len(text) <= 300 for text in parents.values())
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_default_token_chunking_stays_below_the_embedding_window():
    parsed = parse_document(
        "long.md", ("# 高血压\n\n" + "高血压患者应规律随访。" * 200).encode())
    _, _, chunks = chunk_document(parsed, TopicEmbedder(), token_counter=COUNTER)
    assert chunks
    assert max(chunk.token_count for chunk in chunks) <= 400
    assert all(chunk.token_count <= MAX_CHUNK_TOKENS for chunk in chunks)
    assert len({chunk.parent_id for chunk in chunks}) >= 1


def test_token_chunking_is_deterministic_and_rejects_oversized_limits():
    parsed = parse_document("guide.txt", ("高血压患者应规律随访。" * 80).encode())
    first = [chunk.id for chunk in chunk_document(
        parsed, TopicEmbedder(), max_tokens=100, overlap_tokens=15,
        parent_max_tokens=300, token_counter=COUNTER)[2]]
    second = [chunk.id for chunk in chunk_document(
        parsed, TopicEmbedder(), max_tokens=100, overlap_tokens=15,
        parent_max_tokens=300, token_counter=COUNTER)[2]]
    assert first == second

    with pytest.raises(ValueError, match=str(MAX_CHUNK_TOKENS)):
        chunk_document(parsed, TopicEmbedder(), max_tokens=MAX_CHUNK_TOKENS + 1,
                       token_counter=COUNTER)
    with pytest.raises(ValueError, match="重叠"):
        chunk_document(parsed, TopicEmbedder(), max_tokens=120, overlap_tokens=120,
                       token_counter=COUNTER)
    with pytest.raises(ValueError, match="父块"):
        chunk_document(parsed, TopicEmbedder(), max_tokens=120, overlap_tokens=10,
                       parent_max_tokens=100, token_counter=COUNTER)


def test_pdf_causal_sections_and_orphan_page_numbers_preserve_clean_text():
    from medical_rag.pdf_sections import format_chunk_for_index, is_administrative_boilerplate

    pdf = fitz.open()
    p1 = pdf.new_page()
    p1.insert_text((72, 72), "目 录\n一、高血压概述 ........ 2\n二、药物治疗 ........ 5\n三、随访 ........ 8\n四、附录 ........ 12\n1", fontname="china-s")
    p2 = pdf.new_page()
    p2.insert_text((72, 72), "2\n一、高血压概述\n（一）诊断标准\n诊室血压收缩压>=140 mmHg。\n- 2 -", fontname="china-s")
    p3 = pdf.new_page()
    p3.insert_text((72, 72), "参考文献\n[1] 作者A. 文章1.\n[2] 作者B. 文章2.\n[3] 作者C. 文章3.", fontname="china-s")
    parsed = parse_document("中国高血压防治指南（2024年修订版）.pdf", pdf.tobytes())
    _, _, chunks = chunk_document(parsed, TopicEmbedder(), token_counter=COUNTER)

    p2_chunk = next(chunk for chunk in chunks if chunk.page == 2)
    assert p2_chunk.section == "一、高血压概述 / （一）诊断标准"
    assert not p2_chunk.text.lstrip().startswith("2\n")
    assert "- 2 -" not in p2_chunk.text
    assert is_administrative_boilerplate(next(c for c in chunks if c.page == 1).text, "目录") is True
    assert is_administrative_boilerplate(p2_chunk.text, p2_chunk.section) is False

    indexed = format_chunk_for_index(p2_chunk.filename, p2_chunk.section, p2_chunk.text)
    assert indexed.startswith("《中国高血压防治指南（2024年修订版）》 (一、高血压概述 / （一）诊断标准)\n")
    assert "诊室血压收缩压" in indexed


def test_document_embed_adds_metadata_only_to_embedder_input():
    from medical_rag.types import Chunk
    from medical_rag.workflow.contracts import ExecutionContext
    from medical_rag.workflow.nodes.documents import embed

    recorded: list[list[str]] = []

    class CaptureEmbedder:
        def encode(self, texts):
            recorded.append(list(texts))
            return [[0.5, 0.5] for _ in texts]

    chunk = Chunk("c1", "d1", "s1", "氨氯地平2.5~10 mg", "高血压指南.pdf", "常用降压药", 1, 0)
    ctx = ExecutionContext("r1", "document-ingestion", {})
    plain = embed({"chunks": [chunk]}, {}, ctx, CaptureEmbedder())
    titled = embed({"chunks": [chunk]}, {"include_metadata": True}, ctx, CaptureEmbedder())

    assert plain["vectors"] == [[0.5, 0.5]]
    assert titled["vectors"] == [[0.5, 0.5]]
    assert recorded[0] == ["氨氯地平2.5~10 mg"]
    assert recorded[1] == ["《高血压指南》 (常用降压药)\n氨氯地平2.5~10 mg"]
    assert chunk.text == "氨氯地平2.5~10 mg"
