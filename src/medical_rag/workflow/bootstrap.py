"""Register reusable medical RAG node adapters."""

from functools import partial

from .contracts import PortSpec
from .nodes import documents, evidence, generation, input as input_nodes, merge, output, question, retrieval
from .nodes.base import BuiltinNode
from .registry import NodeRegistry


def build_registry(store: object, embedder: object, reranker: object,
                   generator: object, settings: object = None) -> NodeRegistry:
    registry = NodeRegistry()
    candidate_k = getattr(settings, "candidate_k", 20)
    rerank_k = getattr(settings, "rerank_k", 15)
    threshold = getattr(settings, "evidence_threshold", 0.5)
    top_k = getattr(settings, "top_k", 5)
    default_provider = getattr(settings, "provider", "qwen")
    transport = getattr(generator, "transport", None)
    # provider 是这次调用的配置而不是必须连线的数据：所有会用模型的节点都能
    # 在不接 provider 的情况下运行，缺省用运行时的默认厂商。
    provider_port = PortSpec("provider", "string", optional=True)
    # 路由条件写在节点实例的 activation 上，所以被路由的节点只需暴露一个可不接的
    # route 端口：接了 router 就按路由执行，不接就无条件执行。
    route_port = PortSpec("route", "selector", optional=True)
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
                    {"sources": PortSpec("sources", "evidence_list", collect=True)},
                    {"evidence": "evidence_list"}, retrieval.rrf,
                    {"k": {"type": "integer", "default": 60, "minimum": 1}}),
        BuiltinNode("retrieval.rerank", "重排", "rerank",
                    {"question": "string",
                     "top_k": PortSpec("top_k", "integer", optional=True),
                     "evidence": "evidence_list"},
                    {"evidence": "evidence_list"}, partial(retrieval.rerank, reranker=reranker,
                                                              threshold=threshold, rerank_k=rerank_k,
                                                              top_k=top_k),
                    {"top_k": {"type": "integer", "default": top_k, "minimum": 1, "maximum": 20},
                     "threshold": {"type": "number", "default": threshold, "minimum": 0, "maximum": 1},
                     "rerank_k": {"type": "integer", "default": rerank_k, "minimum": 1}}),
        BuiltinNode("question.router", "问题路由", "question",
                    {"question": "string", "provider": provider_port}, {"route": "selector"},
                    partial(question.route_question, transport=transport,
                            default_provider=default_provider)),
        BuiltinNode("question.rewrite", "问题重写", "question",
                    {"question": "string", "provider": provider_port, "route": route_port},
                    {"query": "string"},
                    partial(question.rewrite, transport=transport,
                            default_provider=default_provider)),
        BuiltinNode("question.decompose", "复杂问题分解", "question",
                    {"question": "string", "provider": provider_port, "route": route_port},
                    {"queries": "list<string>"},
                    partial(question.decompose, transport=transport,
                            default_provider=default_provider)),
        BuiltinNode("question.hyde", "假设性回答", "question",
                    {"question": "string", "provider": provider_port, "route": route_port},
                    {"query": "string"},
                    partial(question.hyde, transport=transport, default_provider=default_provider)),
        BuiltinNode("question.stepback", "抽象问题", "question",
                    {"question": "string", "provider": provider_port, "route": route_port},
                    {"query": "string"},
                    partial(question.stepback, transport=transport, default_provider=default_provider)),
        BuiltinNode("merge.collect", "合流检索问题", "question",
                    {"items": PortSpec("items", "string", optional=True, collect=True)},
                    {"values": "list<string>"}, merge.collect),
        BuiltinNode("retrieval.multi", "多查询检索", "retrieval",
                    {"queries": PortSpec("queries", "string", collect=True)},
                    {"evidence": "evidence_list"},
                    partial(retrieval.multi, store=store, embedder=embedder,
                            candidate_k=candidate_k),
                    {"strategy": {"type": "string", "default": "hybrid",
                                  "enum": ["hybrid", "dense", "bm25"]},
                     "k": {"type": "integer", "default": 60, "minimum": 1}}),
        BuiltinNode("evidence.gate", "证据门控", "evidence", {"evidence": "evidence_list"},
                    {"evidence": "evidence_list", "abstained": "boolean", "answer": "string",
                     "citations": "citations"},
                    partial(evidence.gate, threshold=threshold),
                    {"threshold": {"type": "number", "default": threshold, "minimum": 0, "maximum": 1}},
                    tags=frozenset({"evidence.gated"})),
        BuiltinNode("generation.llm", "模型回答", "generation",
                    {"question": "string", "provider": provider_port,
                     "evidence": PortSpec("evidence", "evidence_list",
                                          requires_tag="evidence.gated")},
                    {"draft": "string", "evidence": "evidence_list"},
                    partial(generation.draft, generator=generator,
                            default_provider=default_provider)),
        BuiltinNode("citation.validate", "引用校验", "citation",
                    {"draft": "string", "evidence": "evidence_list"},
                    {"answer": "answer"}, partial(generation.validate, generator=generator),
                    tags=frozenset({"answer.cited"}),
                    shared_producers=(("draft", "evidence"),)),
        BuiltinNode("answer.output", "答案输出", "output",
                    {"answer": PortSpec("answer", "answer", requires_tag="answer.cited")},
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
