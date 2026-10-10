"""Register reusable medical RAG node adapters."""

from functools import partial

from .nodes import documents, evidence, generation, input as input_nodes, output, question, retrieval
from .nodes.base import BuiltinNode
from .nodes.question import RoutedQuestionNode
from .registry import NodeRegistry


def build_registry(store: object, embedder: object, reranker: object,
                   generator: object, settings: object = None) -> NodeRegistry:
    registry = NodeRegistry()
    candidate_k = getattr(settings, "candidate_k", 20)
    rerank_k = getattr(settings, "rerank_k", 15)
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
                                                              threshold=threshold, rerank_k=rerank_k),
                    {"threshold": {"type": "number", "default": threshold, "minimum": 0, "maximum": 1},
                     "rerank_k": {"type": "integer", "default": rerank_k, "minimum": 1}}),
        BuiltinNode("question.router", "问题路由", "question",
                    {"question": "string", "provider": "string"}, {"route": "string"},
                    partial(question.route_question, transport=getattr(generator, "transport", None))),
        RoutedQuestionNode("question.rewrite", "问题重写", "question",
                           {"question": "string", "provider": "string", "route": "string"},
                           {"variant": "query_variant"},
                           partial(question.rewrite, transport=getattr(generator, "transport", None))),
        RoutedQuestionNode("question.decompose", "复杂问题分解", "question",
                           {"question": "string", "provider": "string", "route": "string"},
                           {"variant": "query_variant"},
                           partial(question.decompose, transport=getattr(generator, "transport", None))),
        RoutedQuestionNode("question.hyde", "假设性回答", "question",
                           {"question": "string", "provider": "string", "route": "string"},
                           {"variant": "query_variant"},
                           partial(question.hyde, transport=getattr(generator, "transport", None))),
        RoutedQuestionNode("question.stepback", "抽象问题", "question",
                           {"question": "string", "provider": "string", "route": "string"},
                           {"variant": "query_variant"},
                           partial(question.stepback, transport=getattr(generator, "transport", None))),
        BuiltinNode("question.select", "选择检索问题", "question",
                    {"question": "string", "route": "string", "rewrite": "query_variant",
                     "decompose": "query_variant", "hyde": "query_variant",
                     "stepback": "query_variant"}, {"query_plan": "query_plan"}, question.select),
        BuiltinNode("retrieval.expanded", "扩展检索融合", "retrieval",
                    {"query_plan": "query_plan"}, {"evidence": "evidence_list"},
                    partial(retrieval.expanded, store=store, embedder=embedder,
                            candidate_k=candidate_k),
                    {"k": {"type": "integer", "default": 60, "minimum": 1}}),
        BuiltinNode("evidence.gate", "证据门控", "evidence", {"evidence": "evidence_list"},
                    {"evidence": "evidence_list", "abstained": "boolean", "answer": "string",
                     "citations": "citations"},
                    partial(evidence.gate, threshold=threshold),
                    {"threshold": {"type": "number", "default": threshold, "minimum": 0, "maximum": 1}}),
        BuiltinNode("generation.llm", "模型回答", "generation",
                    {"question": "string", "provider": "string", "evidence": "evidence_list"},
                    {"draft": "string", "evidence": "evidence_list"},
                    partial(generation.draft, generator=generator)),
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
