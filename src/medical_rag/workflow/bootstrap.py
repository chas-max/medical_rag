"""Register reusable medical RAG node adapters."""

from functools import partial
from pathlib import Path
from threading import RLock

from ..question_resolver import QuestionResolver
from ..tokenization import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_PARENT_TOKENS,
    MAX_CHUNK_TOKENS,
    default_token_counter,
)
from .contracts import PortSpec
from .nodes import (
    documents,
    evidence,
    generation,
    merge,
    output,
    planning,
    question,
    retrieval,
)
from .nodes import input as input_nodes
from .nodes.base import BuiltinNode
from .registry import NodeRegistry


def build_registry(store: object, embedder: object, reranker: object,
                   generator: object, settings: object = None,
                   repository: object = None,
                   conversation_history: object = None) -> NodeRegistry:
    registry = NodeRegistry()
    resource_lock = RLock()
    candidate_k = getattr(settings, "candidate_k", 20)
    rerank_k = getattr(settings, "rerank_k", 15)
    threshold = getattr(settings, "evidence_threshold", 0.5)
    rerank_fail_closed = getattr(settings, "rerank_fail_closed", True)
    top_k = getattr(settings, "top_k", 5)
    chunk_max_tokens = getattr(settings, "chunk_max_tokens", DEFAULT_CHUNK_TOKENS)
    chunk_overlap_tokens = getattr(settings, "chunk_overlap_tokens", DEFAULT_OVERLAP_TOKENS)
    parent_max_tokens = getattr(settings, "parent_max_tokens", DEFAULT_PARENT_TOKENS)
    token_counter = default_token_counter()
    default_provider = getattr(settings, "provider", "qwen")
    transport = getattr(generator, "transport", None)
    # One bounded resolver per registry: the resolve node never builds a client and
    # never keeps per-request state on itself.
    resolver = QuestionResolver(transport, default_provider)
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
        BuiltinNode("retrieval.hyde", "假设性回答检索", "retrieval",
                    {"question": "string", "provider": provider_port},
                    {"evidence": "evidence_list"}, partial(retrieval.hyde, store=store,
                        embedder=embedder, transport=transport, default_provider=default_provider,
                        candidate_k=candidate_k, enabled=getattr(settings, "hyde_enabled", True),
                        timeout_seconds=getattr(settings, "hyde_timeout_seconds", 10.0),
                        resource_lock=resource_lock)),
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
        BuiltinNode("question.resolve", "结构化问题消解", "question",
                    {"question": "string", "provider": provider_port},
                    {"query": "string"},
                    partial(question.resolve_question, resolver=resolver)),
        BuiltinNode("question.router", "问题路由", "question",
                    {"question": "string", "provider": provider_port}, {"route": "selector"},
                    partial(question.route_question, transport=transport,
                            default_provider=default_provider)),
        BuiltinNode("question.pre_route", "问题前置路由", "question",
                    {"question": "string"}, {"route": "selector", "reason": "string"},
                    question.pre_route,
                    {"require_context": {"type": "boolean", "default": True}}),
        BuiltinNode("question.rewrite", "问题重写", "question",
                    {"question": "string", "provider": provider_port, "route": route_port,
                     "history": PortSpec("history", "history_context", optional=True)},
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
                    partial(evidence.gate, threshold=threshold,
                            fail_closed=rerank_fail_closed),
                    {"threshold": {"type": "number", "default": threshold, "minimum": 0, "maximum": 1}},
                    tags=frozenset({"evidence.gated"})),
        BuiltinNode("answer.plan", "回答规划", "generation",
                    {"question": "string", "provider": provider_port,
                     "evidence": PortSpec("evidence", "evidence_list", optional=True)},
                    {"plan": "string"},
                    partial(planning.plan, transport=transport,
                            default_provider=default_provider),
                    {"preset": {"type": "string", "default": "auto",
                                "enum": ["auto", "standard", "off"]}}),
        BuiltinNode("generation.llm", "模型回答", "generation",
                    {"question": "string", "provider": provider_port,
                     "plan": PortSpec("plan", "string", optional=True),
                     "history": PortSpec("history", "history_context", optional=True),
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
                    partial(documents.chunk, embedder=embedder, token_counter=token_counter,
                            max_tokens=chunk_max_tokens, overlap_tokens=chunk_overlap_tokens,
                            parent_max_tokens=parent_max_tokens),
                    {"max_tokens": {"type": "integer", "default": chunk_max_tokens,
                                    "minimum": 100, "maximum": MAX_CHUNK_TOKENS},
                     "overlap_tokens": {"type": "integer", "default": chunk_overlap_tokens,
                                        "minimum": 0},
                     "parent_max_tokens": {"type": "integer", "default": parent_max_tokens,
                                           "minimum": 100, "maximum": 4000}}),
        BuiltinNode("document.embed", "文档向量化", "document", {"chunks": "chunks"},
                    {"vectors": "vectors"}, partial(documents.embed, embedder=embedder)),
        BuiltinNode("document.store", "文档入库", "document",
                    {"document": "document", "chunks": "chunks", "vectors": "vectors"},
                    {"result": "ingest_result"}, partial(documents.store, database=store)),
        BuiltinNode("document.output", "入库结果", "output", {"result": "ingest_result"},
                    {"document_id": "string", "chunk_count": "integer", "strategy": "string",
                     "reason": "string", "duplicate": "boolean"}, output.document),
    ]
    # A single registry lock serializes shared store/embedder/reranker access,
    # including ordinary runs overlapping late correction workers.
    for node in nodes:
        if node.type in {"retrieval.dense", "retrieval.bm25", "retrieval.multi", "retrieval.rrf",
                         "retrieval.rerank", "merge.collect"}:
            node.workflow_kinds = ("workflow", "agent_iteration")
        if node.type in {"retrieval.dense", "retrieval.bm25", "retrieval.multi", "retrieval.rerank",
                         "document.chunk", "document.embed", "document.store"}:
            node.worker_lock = resource_lock
        registry.register(node)
    from .agentic import AgenticNode
    from .corrective import CorrectiveNode
    from .nodes.agent import child_nodes
    from .repository import WorkflowRepository
    if repository is None:
        repository = WorkflowRepository(Path(__file__).resolve().parents[3] / "workflows")
    for node in child_nodes(transport, default_provider):
        registry.register(node)
    # The corrective node composes the retrieval handlers above, so it is created
    # after they exist in the registry.
    registry.register(CorrectiveNode(registry, transport, default_provider,
                                     threshold=threshold, top_k=top_k,
                                     history=conversation_history))
    registry.register(AgenticNode(repository, registry))
    return registry
