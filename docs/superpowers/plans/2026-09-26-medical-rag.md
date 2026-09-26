# Medical RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a testable FastAPI service for ingesting public Chinese medical documents and answering with retrieved citations.

**Architecture:** Parsing and chunking, embedding, Milvus persistence, retrieval/reranking, and grounded generation are separate units with injected dependencies. All public routes use one response envelope; offline API tests use in-memory fakes while optional integration tests target Milvus Standalone.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, pypdf, sentence-transformers, pymilvus, OpenAI-compatible HTTP, pytest, Docker Compose Milvus.

## Global Constraints

- Input: Markdown, TXT, text-extractable PDF; exclude scans, URL, images, private patient records, independent frontend.
- Embedding: `BAAI/bge-small-zh-v1.5`, dimension 512. Dense index `HNSW_PQ`/`COSINE` with `M=16`, `efConstruction=200`, `m=64`, `nbits=4`.
- BM25: Milvus built-in function and `SPARSE_INVERTED_INDEX`, Chinese analyzer, Milvus >=2.5.11. Never silently substitute another index.
- Reranker: `BAAI/bge-reranker-v2-m3`; RRF `k=60`; normalized score threshold initially 0.5.
- Generation defaults: DeepSeek `deepseek-flash` and Qwen `qwen3.8-flash`; independently configurable keys and endpoints. No external paid API in default tests.
- Responses always `{success, data, error}`. Fail closed without evidence or valid citations. Tests first; target >80% coverage.
- Current workstation has Python 3.13 but no Docker executable; report genuine Milvus integration as unverified until Docker is available.

## File map

- `pyproject.toml`, `.env.example`, `.gitignore`: dependencies, settings example, exclusions.
- `src/medical_rag/types.py`, `config.py`: shared value objects and validated configuration.
- `src/medical_rag/ingest.py`: parsers, three chunkers, automatic router and metadata.
- `src/medical_rag/embeddings.py`: lazy BGE embedding and rerank adapters.
- `src/medical_rag/store.py`: Milvus schema, HNSW_PQ/BM25 indices, persistence and search.
- `src/medical_rag/retrieval.py`: fusion, reranking, evidence threshold.
- `src/medical_rag/generation.py`: DeepSeek/Qwen transport and citation validation.
- `src/medical_rag/service.py`, `api.py`: orchestration and HTTP envelope.
- `tests/`: API/ingestion/retrieval/generation tests with fakes; opt-in Milvus integration test.
- `docker-compose.yml`, `README.md`: service dependencies and usage.

### Task 1: Project contract and API shell

**Files:** Create `pyproject.toml`, `.gitignore`, `.env.example`, `src/medical_rag/__init__.py`, `src/medical_rag/types.py`, `src/medical_rag/config.py`, `src/medical_rag/api.py`, `tests/test_api.py`.

**Interfaces:** Produce `Settings`, `Document`, `Chunk`, `Evidence`, `ApiError`, and `create_app(service=None)` for all later tasks. `service` has `ingest(filename,content,source_id)`, `list_documents()`, `search(question,top_k)`, `answer(question,provider,top_k)`, `health()` methods.

- [ ] **Step 1: Write failing API tests** in `tests/test_api.py` with `TestClient(create_app(service=FakeService()))`: assert `/health` returns envelope, invalid `top_k=0` is 422 envelope, unsupported upload suffix is 415 envelope, and `/answers` unknown provider is 422. FakeService returns literal dictionaries and records calls.
- [ ] **Step 2: Verify red:** `python -m pytest tests/test_api.py -q`; expect import failure for `medical_rag.api` or failing envelope assertions.
- [ ] **Step 3: Implement contracts and API:** `Settings` uses `pydantic_settings.BaseSettings` with `milvus_uri`, `collection_name`, `deepseek_api_key`, `qwen_api_key`, per-provider model and URL, `candidate_k=20`, `top_k=5`, `max_upload_bytes=20_000_000`, `evidence_threshold=0.5`; validate positive limits. `Document(id,source_id,filename,sha256,chunk_count,strategy,reason)`, `Chunk(id,document_id,source_id,text,filename,section,page,ordinal)`, `Evidence(chunk,score,fused_score)` are dataclasses. Implement five specified FastAPI routes with Pydantic request objects, upload suffix/size checks, exception handler emitting `{success:false,data:null,error:{code,message}}`, and success helper returning `{success:true,data:...,error:null}`. Defer real service construction to Task 5; app factory raises an explicit configuration error only when invoked without service before Task 5.
- [ ] **Step 4: Verify green:** `python -m pytest tests/test_api.py -q`; expect 4 passing tests. Run `python -m compileall -q src`.
- [ ] **Step 5: Commit:** `git add pyproject.toml .gitignore .env.example src tests/test_api.py` then `git commit -m "feat: establish API contract and project configuration"`.

### Task 2: Parse and route public documents

**Files:** Create `src/medical_rag/ingest.py`, `tests/test_ingest.py`.

**Interfaces:** Produce `ParsedDocument(filename,sha256,paragraphs)` and `Paragraph(text,page,section)`, `parse_document(filename,data)->ParsedDocument`, `chunk_document(doc,embedder,max_chars=700,overlap=80)->tuple[str,str,list[Chunk]]`. Embedder exposes `encode(list[str])->list[list[float]]`.

- [ ] **Step 1: Write failing tests** for Markdown heading preservation, TXT with topic-shifting paragraphs choosing semantic, short TXT choosing recursive, text PDF page preservation, blank PDF rejection, and no lost non-whitespace characters across recursive splitting. Assert every chunk length <=700 and stable IDs across repeat calls.
- [ ] **Step 2: Verify red:** `python -m pytest tests/test_ingest.py -q`; expect missing parser/chunker behavior.
- [ ] **Step 3: Implement:** Parse PDF page by page using `pypdf.PdfReader(BytesIO(data))`; decode TXT/MD as UTF-8 including BOM and reject invalid encoding. Normalize CRLF and redundant whitespace without deleting words. Markdown split on ATX headings, carry current heading into paragraph metadata; split long sections recursively by paragraph/sentence/character, adding overlap. Semantic route only if >=3 substantive paragraphs and adjacent normalized embedding cosine falls below configurable boundary (initially 0.65); do not embed for empty/short input. PDF cannot cross page boundary; every output chunk inherits its page. Make IDs `sha256(document_hash + ':' + ordinal + ':' + text)`; return strategy and human-readable reason.
- [ ] **Step 4: Verify green:** `python -m pytest tests/test_ingest.py -q`; expect all tests passing, including parser rejection.
- [ ] **Step 5: Commit:** `git add src/medical_rag/ingest.py tests/test_ingest.py` then `git commit -m "feat: parse and automatically chunk medical documents"`.

### Task 3: Model adapters and Milvus persistence

**Files:** Create `src/medical_rag/embeddings.py`, `src/medical_rag/store.py`, `tests/test_store.py`, `tests/test_embeddings.py`.

**Interfaces:** `BgeEmbedder.encode(texts)->list[list[float]]`, `BgeReranker.score(question,texts)->list[float]`; `MilvusStore.ensure_schema()`, `replace(document,chunks,vectors)->bool`, `list_documents()->list[Document]`, `search_dense(vector,limit)->list[Evidence]`, `search_bm25(question,limit)->list[Evidence]`, `ping()->bool`. Inject `MilvusClient` into store for testability.

- [ ] **Step 1: Write failing tests:** use a recording client to assert schema contains `VARCHAR/TEXT` with analyzer `chinese`, `SPARSE_FLOAT_VECTOR` and BM25 `Function`, dense `HNSW_PQ`/`COSINE` params exactly as above, sparse `SPARSE_INVERTED_INDEX`/`BM25`; existing incompatible schema errors without dropping collection. Test duplicate hash returns true without writes, failed insert preserves existing blocks, vectors of wrong dimension/NaN rejected. Test BGE adapters with injected encoder/cross-encoder for shape validation and normalized rerank scores.
- [ ] **Step 2: Verify red:** `python -m pytest tests/test_store.py tests/test_embeddings.py -q`; expect missing adapters/store.
- [ ] **Step 3: Implement:** Lazy-load `SentenceTransformer` and `CrossEncoder` only on first use, normalize embeddings, validate 512 finite elements; return sigmoid-normalized cross-encoder scores. Use `MilvusClient.create_schema`, `FunctionType.BM25`, `DataType.SPARSE_FLOAT_VECTOR` and `prepare_index_params` for two indices; use client `search` separately on dense and sparse fields, output text/source fields. For updates insert all new IDs before deleting prior IDs, with error/cleanup path; list unique documents via stored metadata without introducing a second DB. Surface incompatible schema/index and write failures as typed errors, never recreate collection.
- [ ] **Step 4: Verify green:** `python -m pytest tests/test_store.py tests/test_embeddings.py -q`; expect all tests passing.
- [ ] **Step 5: Commit:** `git add src/medical_rag/embeddings.py src/medical_rag/store.py tests/test_store.py tests/test_embeddings.py` then `git commit -m "feat: persist dense and BM25 evidence in Milvus"`.

### Task 4: Hybrid retrieval and grounded generation

**Files:** Create `src/medical_rag/retrieval.py`, `src/medical_rag/generation.py`, `tests/test_retrieval.py`, `tests/test_generation.py`.

**Interfaces:** `Retriever.search(question,top_k)->list[Evidence]`; `GroundedGenerator.answer(question,evidence,provider)->dict` returning `answer`, `citations`, `abstained`. Generator transport injected as `complete(provider,system,user)->str`.

- [ ] **Step 1: Write failing tests:** duplicate hits fuse into one evidence with RRF `1/(60+rank)` sum; reranker receives only fused candidate text and order follows rerank scores; empty/low-scoring evidence never calls transport; valid generated JSON citation has source ID and exact excerpt; unknown citation, missing citation, invented excerpt, malformed JSON and missing provider key fail closed. Verify DeepSeek and Qwen model IDs are independently selected.
- [ ] **Step 2: Verify red:** `python -m pytest tests/test_retrieval.py tests/test_generation.py -q`; expect absent retrieval/generation functions.
- [ ] **Step 3: Implement:** Query dense and BM25 separately with per-leg candidate limit, RRF merge by chunk ID, rerank candidates and filter by threshold. Use OpenAI-compatible client with separate base URL/API key and explicit model for each provider; disable web search. In a system prompt require answers only from numbered evidence and strict JSON `{answer,citations:[{chunk_id,excerpt}]}`. Parse JSON, validate nonblank answer, at least one citation, all IDs from supplied evidence, exact substring excerpts, and cited evidence present; otherwise return fixed Chinese abstention. Note this validator cannot prove semantic entailment.
- [ ] **Step 4: Verify green:** `python -m pytest tests/test_retrieval.py tests/test_generation.py -q`; expect all tests passing.
- [ ] **Step 5: Commit:** `git add src/medical_rag/retrieval.py src/medical_rag/generation.py tests/test_retrieval.py tests/test_generation.py` then `git commit -m "feat: fuse, rerank and cite retrieved evidence"`.

### Task 5: Wire service and end-to-end API tests

**Files:** Create `src/medical_rag/service.py`, `tests/test_service.py`; modify `src/medical_rag/api.py`, `tests/test_api.py`.

**Interfaces:** `RagService(settings,store,embedder,reranker,generator)` implements Task 1 service contract. `create_app()` constructs dependencies on startup/lifespan, while `create_app(service=fake)` avoids model/Milvus startup in tests.

- [ ] **Step 1: Write failing integration-style API test** with fake store/embedder/reranker/transport: upload Markdown, list document, search it, answer with verified excerpt, repeat same upload and assert no duplicate rows. Also assert out-of-scope query abstains without transport call and service errors use stable codes.
- [ ] **Step 2: Verify red:** `python -m pytest tests/test_service.py tests/test_api.py -q`; expect missing real orchestration.
- [ ] **Step 3: Implement:** `RagService.ingest` parses, chunks, embeds, replaces and returns strategy/reason; `search` validates question and delegates; `answer` uses search and grounded generator; `health` calls store ping. In app factory construct settings and lazy adapters, initialize schema during lifespan. Translate `ValueError` to 422, unsupported media to 415, oversized payload to 413, dependency failures to 503. Preserve `{success,data,error}` even for FastAPI validation failures.
- [ ] **Step 4: Verify green and regression:** `python -m pytest -q --cov=medical_rag --cov-report=term-missing`; expect >80% and no failures; `python -m compileall -q src`.
- [ ] **Step 5: Commit:** `git add src/medical_rag/service.py src/medical_rag/api.py tests` then `git commit -m "feat: expose complete RAG workflow through FastAPI"`.

### Task 6: Local operations and optional real integration

**Files:** Create `docker-compose.yml`, `README.md`, `tests/test_milvus_integration.py`; modify `.env.example` with Milvus variables and `pyproject.toml` with the verified optional integration test marker.

**Interfaces:** `uvicorn medical_rag.api:app --app-dir src --reload` serves `/docs`; `RUN_MILVUS_INTEGRATION=1` enables real collection test with isolated collection name.

- [ ] **Step 1: Write opt-in test** that creates a uniquely named test collection, verifies both indices, inserts enough distinct vectors for PQ training, confirms Chinese BM25 and dense retrieval, and deletes only its own exact test collection in a `finally` block. Skip cleanly unless `RUN_MILVUS_INTEGRATION=1` and Milvus URI set.
- [ ] **Step 2: Verify red/skip:** `python -m pytest tests/test_milvus_integration.py -q`; expect skip without Docker; with Docker enabled, expect failure until schema works.
- [ ] **Step 3: Add Compose and README:** Pin compatible Milvus Standalone and its etcd/MinIO dependencies, persistent named volumes, documented ports. README includes Windows PowerShell venv, `pip install -e ".[dev]"`, `docker compose up -d`, model download/cache, environment variables, API examples, `pytest`, coverage and health checks; warn no PHI and no medical advice. Do not commit secrets. If Docker unavailable, do not claim real integration passed.
- [ ] **Step 4: Verify:** `python -m pytest -q --cov=medical_rag --cov-report=term-missing`, `python -m compileall -q src`, `git diff --check`; if Docker is present, `docker compose config`, `docker compose up -d`, then `RUN_MILVUS_INTEGRATION=1 python -m pytest tests/test_milvus_integration.py -q`; otherwise report Docker-dependent checks unverified.
- [ ] **Step 5: Commit:** `git add docker-compose.yml README.md .env.example pyproject.toml tests/test_milvus_integration.py` then `git commit -m "docs: provide reproducible Milvus setup and integration test"`.
