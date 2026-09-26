# RAG Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in offline evaluation CLI for retrieval metrics and model-judged faithfulness, context relevance and answer relevance.

**Architecture:** Pure ranking metrics consume ordered chunk IDs and relevance labels. An injected judge calls the existing provider transport with strict JSON scoring; an evaluator coordinates service calls and aggregates reports. A CLI validates the complete dataset before constructing live dependencies, then writes JSON only after evaluation.

**Tech Stack:** Python 3.11+, pytest, existing Pydantic Settings, OpenAI-compatible transport, Milvus-backed RagService.

## Global Constraints

- Work in `D:/project_rag/.worktrees/medical-rag` on `feat/medical-rag`; preserve the main checkout and unrelated changes.
- No new public HTTP endpoint, automatic upload, normal search/answer behavior change or paid external calls in tests.
- JSONL requires nonempty `question`, nonempty unique `relevant_chunk_ids`, optional unique `id`; validate all lines before any model calls.
- Rank metrics use the final threshold-filtered search output. Precision denominator is returned count; AP denominator is `min(len(relevant), k)`.
- Abstention or answer failure leaves all semantic scores null. An individual judge failure leaves only that metric null, records a sanitized error and continues.
- Explicit CLI invocation is the only trigger for paid API calls. Never persist keys, tokens or raw service errors in reports.
- Use tests first, target project coverage above 80%, and update README with usage, limitations and costs.

---

## File map

- Create `src/medical_rag/evaluation_metrics.py`: immutable retrieval score and pure per-query/aggregate functions.
- Create `src/medical_rag/evaluation_judge.py`: strict score parsing and three dimension-specific judge methods.
- Create `src/medical_rag/evaluation.py`: dataset parsing, evaluator orchestration and report aggregation.
- Create `src/medical_rag/evaluate.py`: CLI and live component construction.
- Create `tests/test_evaluation_metrics.py`, `tests/test_evaluation_judge.py`, `tests/test_evaluation.py`, `tests/test_evaluate_cli.py`.
- Modify `README.md` for input, run, result, safety; use explicit user-chosen dataset/output paths instead of broad ignore rules.

### Task 1: Retrieval metrics

**Files:** Create `src/medical_rag/evaluation_metrics.py`, `tests/test_evaluation_metrics.py`.

**Interfaces:** `retrieval_scores(ranked_ids: list[str], relevant_ids: set[str], k: int) -> dict[str, object]` returns `precision`, `recall`, `f1`, `ap`, `rr`, `retrieved_count`, `hit_ids`; `mean_or_none(values: list[float]) -> float | None` is used in Task 3.

- [ ] **Step 1: Write failing tests:** assert ranking `["x","a","b"]`, relevance `{a,b,c}`, k=3 yields precision 2/3, recall 2/3, F1 2/3, AP `(1/2+2/3)/3`, RR 1/2 and hit IDs `[a,b]`; also empty ranking yields zeros, duplicate result IDs are ignored, k=1 caps ranking, invalid k/relevance raises ValueError, `mean_or_none([]) is None`.
- [ ] **Step 2: Verify red:** `python -m pytest tests/test_evaluation_metrics.py -q`; expected failure is absent module/function.
- [ ] **Step 3: Implement minimal code:** reject `k < 1` or empty relevant set; dedupe `ranked_ids[:k]` in order; for every hit accumulate precision at its one-based position, first-hit reciprocal rank, then compute counts and F1 with zero denominator handling; `ap = precision_sum / min(len(relevant_ids), k)`; average only supplied finite scores in `mean_or_none`, returning null on empty.
- [ ] **Step 4: Verify green:** run that file, then `python -m pytest -q`; expect all existing tests pass.
- [ ] **Step 5: Commit:** `git add src/medical_rag/evaluation_metrics.py tests/test_evaluation_metrics.py` and `git commit -m "feat: compute RAG retrieval ranking metrics"`.

### Task 2: Strict model judge

**Files:** Create `src/medical_rag/evaluation_judge.py`, `tests/test_evaluation_judge.py`.

**Interfaces:** `EvaluationJudge(transport: object, provider: str, model: str)`; `score_faithfulness(question: str, answer: str, contexts: list[str]) -> dict`, `score_context(question: str, context: str) -> dict`, `score_answer(question: str, answer: str) -> dict`. Each result has `score: float`, `reason: str`, `model: str`. Transport exposes `complete(provider, system, user) -> str`.

- [ ] **Step 1: Write failing tests:** fake transport returns `{"score":0.75,"reason":"..."}` and records inputs; assert each dimension sends only its required data and its prompt describes 0/1 anchors and untrusted evidence. Parametrize malformed JSON, missing/extra fields, bool/string/NaN/out-of-range scores, empty reason; require ValueError. Assert contexts are passed as data, not interpolated into system instructions.
- [ ] **Step 2: Verify red:** `python -m pytest tests/test_evaluation_judge.py -q`; expected missing judge.
- [ ] **Step 3: Implement minimal code:** `json.dumps(..., ensure_ascii=False)` user payload; dimension-specific Chinese criteria in system message; `json.loads` and exact `{"score","reason"}` key check, `type(score) in (int,float)`, `math.isfinite`, 0–1 bound and nonblank reason. Include configured model in valid result; no raw malformed output in exceptions.
- [ ] **Step 4: Verify green:** run focused and full pytest; expect all pass without network.
- [ ] **Step 5: Commit:** add the two files and `git commit -m "feat: judge grounded answers and context relevance"`.

### Task 3: Dataset and report orchestration

**Files:** Create `src/medical_rag/evaluation.py`, `tests/test_evaluation.py`.

**Interfaces:** `load_dataset(path: Path) -> list[dict]` validates the whole UTF-8 JSONL file; `evaluate_cases(cases: list[dict], service: object, judge: EvaluationJudge, top_k: int, answer_provider: str, judge_provider: str) -> dict` returns `config`, `cases`, `summary`. Service exposes `search(question,k)` and `generator.answer(question,evidence,provider)`; use the SAME evidence for both generation and scoring, avoiding a second search.

- [ ] **Step 1: Write failing tests:** parse two lines with optional IDs, reject blank dataset, malformed JSON, missing/extra fields, whitespace question, duplicate IDs, empty/duplicate relevant IDs and non-string IDs before service use. Fake service returns ordered evidence; assert retrieval metrics and one answer generation per case, dimension scores and per-context details, macro averages, valid score counts. Test abstention skips judge, one context judge failure leaves context metric null but other dimensions survive, answer failure skips all semantics, and search failure records safe error while continuing next case.
- [ ] **Step 2: Verify red:** `python -m pytest tests/test_evaluation.py -q`; expect missing module.
- [ ] **Step 3: Implement minimal code:** read `Path.read_text(encoding="utf-8")`, validate every nonblank JSON line with strict keys and stable 1-based line ID default; raise a line-numbered ValueError before returning cases. For each case call `service.search` once, score IDs, call `service.generator.answer` directly with this evidence, then judge 3 dimensions; judge each context independently, but mark context mean null if any individual score failed, preserving per-context errors. Catch operational errors without serializing exception text; aggregate numeric means and counts, refusal count/rate, judge failure count and safe per-case error codes. Include question, IDs, answer, retrieved text and scores in report; no secrets.
- [ ] **Step 4: Verify green:** run focused/full pytest; expect all pass.
- [ ] **Step 5: Commit:** add the two files and `git commit -m "feat: run and summarize offline RAG evaluations"`.

### Task 4: CLI, docs and final verification

**Files:** Create `src/medical_rag/evaluate.py`, `tests/test_evaluate_cli.py`; modify `README.md`.

**Interfaces:** `main(argv: list[str] | None = None) -> int`; module guard raises `SystemExit(main())`. CLI arguments: required `--dataset`, `--output`; optional `--top-k` default 5, `--answer-provider` default deepseek, `--judge-provider` default qwen. Choices for providers are deepseek/qwen.

- [ ] **Step 1: Write failing tests:** inject a service factory/runner to avoid Milvus and API calls; assert invalid dataset prevents dependency construction, invalid k returns nonzero, successful run writes UTF-8 indented JSON with summary, failed initialization/write returns nonzero and no fake success. Check output parent must exist and output cannot equal dataset path; existing output overwrite is explicit in docs.
- [ ] **Step 2: Verify red:** `python -m pytest tests/test_evaluate_cli.py -q`; expect missing CLI.
- [ ] **Step 3: Implement minimal code:** argparse with choices and range; `load_dataset` first; resolve output/dataset and reject same path; construct `Settings`, `MilvusClient`, `MilvusStore.ensure_schema`, `BgeEmbedder`, `BgeReranker`, `GroundedGenerator(OpenAITransport(settings))`, `RagService`, `EvaluationJudge(OpenAITransport(settings), judge_provider, configured_model)`; check both configured keys before running; call `evaluate_cases`, serialize with `json.dumps(ensure_ascii=False, indent=2, allow_nan=False)`, write after completed run, print only safe status/errors. Let tests inject a factory rather than launch real dependencies.
- [ ] **Step 4: Update README:** give one-line JSONL sample with ID and relevant block, exact command, key configuration, output structure, formulas, threshold/abstention semantics, cost/privacy warning, model self-judging bias and non-medical-validation warning. No committed live dataset or generated report.
- [ ] **Step 5: Verify green:** `python -m pytest -q --cov=medical_rag --cov-report=term-missing`; `python -m compileall -q src`; `git diff --check`; expect all pass and coverage >80%. Report any unavailable real Milvus/model integration as unverified.
- [ ] **Step 6: Commit:** `git add src/medical_rag/evaluate.py tests/test_evaluate_cli.py README.md` and `git commit -m "feat: expose offline RAG evaluation CLI"`.

## Self-review

- Retrieval denominator, rank order and MAP/MRR are covered in Task 1; strict model judgment in Task 2; validation, abstention and aggregation in Task 3; opt-in CLI and documentation in Task 4.
- Exact filenames and shared interfaces align across tasks. Production tests use injected fakes; the live component construction is limited to the CLI.
