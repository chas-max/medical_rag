"""Register reusable medical RAG node adapters."""

from functools import partial

from .nodes import documents, evidence, generation, input as input_nodes, output, retrieval
from .nodes.base import BuiltinNode
from .registry import NodeRegistry


def build_registry(store: object, embedder: object, reranker: object,
                   generator: object, settings: object = None) -> NodeRegistry:
    registry = NodeRegistry()
    candidate_k = getattr(settings, "candidate_k", 20)
    threshold = getattr(settings, "evidence_threshold", 0.5)
    nodes = [
        BuiltinNode("query.input", "问题输入", "input", {},
                    {"question": "string", "top_k": "integer", "provider": "string"}, input_nodes.query_input),
        BuiltinNode("retrieval.dense", "向量检索", "retrieval", {"question": "string"},
                    {"evidence": "evidence_list"}, partial(retrieval.dense, store=store, embedder=embedder,
                                                              candidate_k=candidate_k),
                    {"top_k": {"type": "integer", "default": candidate_k, "minimum": 1}}),
        BuiltinNode("retrieval.bm25", "关键词检索", "retrieval", {"question": "string"},
                    {"evidence": "evidence_list"}, partial(retrieval.bm25, store=store,
                                                              candidate_k=candidate_k),
                    {"top_k": {"type": "integer", "default": candidate_k, "minimum": 1}}),
        BuiltinNode("retrieval.rrf", "RRF 融合", "fusion",
                    {"dense": "evidence_list", "bm25": "evidence_list"},
                    {"evidence": "evidence_list"}, retrieval.rrf,
                    {"k": {"type": "integer", "default": 60, "minimum": 1}}),
        BuiltinNode("retrieval.rerank", "重排", "rerank",
                    {"question": "string", "top_k": "integer", "evidence": "evidence_list"},
                    {"evidence": "evidence_list"}, partial(retrieval.rerank, reranker=reranker,
                                                              threshold=threshold),
                    {"threshold": {"type": "number", "default": threshold, "minimum": 0, "maximum": 1}}),
        BuiltinNode("evidence.gate", "证据门控", "evidence", {"evidence": "evidence_list"},
                    {"evidence": "evidence_list", "abstained": "boolean", "answer": "string",
                     "citations": "citations"},
                    partial(evidence.gate, threshold=threshold),
                    {"threshold": {"type": "number", "default": threshold, "minimum": 0, "maximum": 1}}),
        BuiltinNode("generation.llm", "模型回答", "generation",
                    {"question": "string", "provider": "string", "evidence": "evidence_list"},
                    {"draft": "string"}, partial(generation.draft, generator=generator)),
        BuiltinNode("citation.validate", "引用校验", "citation",
                    {"draft": "string", "evidence": "evidence_list"},
                    {"answer": "answer"}, partial(generation.validate, generator=generator)),
        BuiltinNode("answer.output", "答案输出", "output", {"answer": "answer"},
                    {"answer": "string", "citations": "citations", "abstained": "boolean"}, output.answer),
        BuiltinNode("search.output", "证据输出", "output", {"evidence": "evidence_list"},
                    {"evidence": "evidence_list"}, output.search),
        BuiltinNode("document.input", "文档输入", "input", {},
                    {"filename": "string", "content": "bytes", "source_id": "string"}, input_nodes.document_input),
        BuiltinNode("document.parse", "文档解析", "document",
                    {"filename": "string", "content": "bytes"}, {"parsed": "parsed_document"}, documents.parse),
        BuiltinNode("document.chunk", "文档分块", "document",
                    {"parsed": "parsed_document", "source_id": "string"},
                    {"document": "document", "chunks": "chunks"},
                    partial(documents.chunk, embedder=embedder)),
        BuiltinNode("document.embed", "文档向量化", "document", {"chunks": "chunks"},
                    {"vectors": "vectors"}, partial(documents.embed, embedder=embedder)),
        BuiltinNode("document.store", "文档入库", "document",
                    {"document": "document", "chunks": "chunks", "vectors": "vectors"},
                    {"result": "ingest_result"}, partial(documents.store, database=store)),
        BuiltinNode("document.output", "入库结果", "output", {"result": "ingest_result"},
                    {"document_id": "string", "chunk_count": "integer", "strategy": "string",
                     "reason": "string", "duplicate": "boolean"}, output.document),
    ]
    for node in nodes:
        registry.register(node)
    return registry
