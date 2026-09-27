# Medical Health Evaluation Dataset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and integrate a versioned 500-question Chinese public-health evaluation set with stable source anchors, runtime chunk remapping, and a platform-neutral judge export format.

**Architecture:** Keep the curated question bank as immutable JSONL metadata keyed by stable source anchors; derive `relevant_chunk_ids` only when the current corpus is ingested. Add a strict dataset module and CLI tools for validation, anchor-to-chunk remapping, and judge export, while extending the existing evaluator to preserve rich metadata and handle explicit `insufficient_evidence` cases. Store the first complete 500-question release under `data/evaluations/medical-health-v1/`.

**Tech Stack:** Python 3.11+, existing `medical_rag` package, JSONL/JSON, pytest, pypdf, SHA-256 hashing, existing evaluation CLI.

## Global Constraints

- The formal release contains exactly 500 questions: 480 `answerable`, 20 `insufficient_evidence`.
- Sources are limited to the seven public documents registered in `data/README.md`.
- The topic quotas are 110 public-health/follow-up, 100 primary-care hypertension, 75 hypertension diet, 75 diabetes diet, 70 health literacy, 50 dietary-guideline principles, and 20 cross-topic scenarios.
- Question quotas are factual 140, procedure 120, numeric/threshold/frequency 70, scenario 100, comparison/trade-off 40, and multi-evidence 30.
- Difficulty quotas are easy 180, medium 230, hard 90.
- Every formal question has a reference answer, nonempty key points, a stable source anchor, a unique ID, and a versioned metadata record.
- `relevant_chunk_ids` are generated from current ingested chunks and never replace stable source references.
- Any unresolved, ambiguous, hash-mismatched, or below-threshold anchor mapping fails the command; the tool must not guess.
- All model-facing judge exports use the same 0–4 rubric and do not include API keys, request headers, chain-of-thought, or raw provider errors.
- Tests do not call Milvus or paid model APIs; live corpus mapping is verified only with deterministic fake chunk inventories.
- Preserve unrelated working-tree changes and never use destructive git commands.

## File Map

- Create `src/medical_rag/evaluation_dataset.py`: schema constants, rich-row validation, quota validation, and dataset loading.
- Modify `src/medical_rag/evaluation.py`: accept rich rows, preserve metadata in reports, and separate `insufficient_evidence` handling from normal retrieval denominators.
- Create `src/medical_rag/evaluation_anchors.py`: source text normalization, anchor ID generation, SHA-256 helpers, and deterministic chunk matching.
- Create `src/medical_rag/evaluation_tools.py`: reusable command functions for dataset validation, chunk remapping, and judge payload export.
- Create `data/evaluations/medical-health-v1/manifest.json`.
- Create `data/evaluations/medical-health-v1/questions.jsonl` with exactly 500 curated rows.
- Create `data/evaluations/medical-health-v1/source-registry.json`.
- Create `data/evaluations/medical-health-v1/rubric.md`.
- Create `data/evaluations/medical-health-v1/README.md`.
- Create `data/evaluations/medical-health-v1/CHANGELOG.md`.
- Create `data/evaluations/medical-health-v1/tools/validate_dataset.py`.
- Create `data/evaluations/medical-health-v1/tools/remap_chunk_ids.py`.
- Create `data/evaluations/medical-health-v1/tools/export_for_judge.py`.
- Create `tests/test_evaluation_dataset.py`, `tests/test_evaluation_anchors.py`, `tests/test_evaluation_tools.py`.
- Modify `tests/test_evaluation.py` and `tests/test_evaluate_cli.py` for rich metadata and explicit abstention semantics.

---

### Task 1: Define the rich dataset contract and backward-compatible loading

**Files:**
- Create: `src/medical_rag/evaluation_dataset.py`
- Modify: `src/medical_rag/evaluation.py`
- Create: `tests/test_evaluation_dataset.py`
- Modify: `tests/test_evaluation.py`

**Interfaces:**
- `load_evaluation_dataset(path: Path, *, expected_count: int | None = None) -> list[dict]`
- `validate_evaluation_row(row: dict, line_number: int, *, allow_empty_relevant: bool = True) -> dict`
- `validate_quotas(cases: list[dict], manifest: dict) -> None`
- `load_dataset(path)` remains available for legacy JSONL and delegates to the new validator with the old minimal return shape when no rich fields exist.

- [ ] **Step 1: Write failing tests**

```python
def test_rich_row_preserves_reference_metadata(tmp_path):
    path = tmp_path / "questions.jsonl"
    path.write_text(json.dumps({
        "id": "MHE-0001", "question": "问题", "reference_answer": "答案",
        "key_points": ["要点"], "answerability": "answerable",
        "category": "基层高血压管理", "question_type": "procedure",
        "difficulty": "medium", "source_refs": [{
            "source_id": "src", "locator": {"page": 1},
            "quote": "原文", "anchor_id": "src#q"
        }], "relevant_chunk_ids": []
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    cases = load_evaluation_dataset(path)
    assert cases[0]["reference_answer"] == "答案"
    assert cases[0]["source_refs"][0]["anchor_id"] == "src#q"


@pytest.mark.parametrize("bad", [
    {"id": "MHE-0001", "question": "q", "reference_answer": "a",
     "key_points": [], "answerability": "answerable", "category": "x",
     "question_type": "fact", "difficulty": "easy", "source_refs": [],
     "relevant_chunk_ids": []},
    {"id": "MHE-0001", "question": "q", "reference_answer": "a",
     "key_points": ["p"], "answerability": "unknown", "category": "x",
     "question_type": "fact", "difficulty": "easy", "source_refs": [],
     "relevant_chunk_ids": []},
])
def test_invalid_rich_rows_fail_before_evaluation(tmp_path, bad):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(bad, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="第 1 行"):
        load_evaluation_dataset(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_evaluation_dataset.py -q`

Expected: FAIL because the rich loader and validators do not yet exist.

- [ ] **Step 3: Implement the contract**

Define exact enums `answerability = {"answerable", "insufficient_evidence"}`, `question_type = {"fact", "procedure", "numeric", "scenario", "comparison", "multi_evidence"}`, and `difficulty = {"easy", "medium", "hard"}`. Require all rich keys, nonempty strings, unique key points, at least one source reference, valid locator mapping, stable anchor IDs, and unique relevant chunk IDs. Permit an empty `relevant_chunk_ids` only for source-form rows before remapping or for `insufficient_evidence`; retain the existing minimal loader behavior for old tests.

Update `evaluate_cases` to copy `reference_answer`, `key_points`, `answerability`, category, type, difficulty, and source references into each result. For `insufficient_evidence`, do not call normal semantic judges; record `expected_abstention`, `correct_abstention`, and `unsupported_answer` fields. Exclude those rows from ordinary recall denominators and add summary counts for the abstention subset.

- [ ] **Step 4: Run focused and regression tests**

Run: `python -m pytest tests/test_evaluation_dataset.py tests/test_evaluation.py -q`

Expected: PASS, including all existing minimal JSONL tests.

- [ ] **Step 5: Commit**

```powershell
git add src/medical_rag/evaluation_dataset.py src/medical_rag/evaluation.py tests/test_evaluation_dataset.py tests/test_evaluation.py
git commit -m "feat: support rich medical evaluation dataset rows"
```

### Task 2: Implement stable source anchors and deterministic chunk remapping

**Files:**
- Create: `src/medical_rag/evaluation_anchors.py`
- Create: `tests/test_evaluation_anchors.py`

**Interfaces:**
- `normalize_source_text(text: str) -> str`
- `source_sha256(path: Path) -> str`
- `make_anchor_id(source_id: str, heading_path: list[str], quote: str) -> str`
- `match_anchor(anchor: dict, chunks: list[dict], *, min_score: float = 0.92) -> dict`

- [ ] **Step 1: Write failing tests**

```python
def test_anchor_id_is_stable_under_whitespace_changes():
    first = make_anchor_id("src", ["第一章", "测量"], "血压  测量前静坐。")
    second = make_anchor_id("src", ["第一章", "测量"], "血压\n测量前静坐。")
    assert first == second


def test_match_anchor_requires_unique_high_confidence_chunk():
    anchor = {"source_id": "src", "quote": "测量前静坐五分钟", "anchor_id": "src#x"}
    chunks = [
        {"id": "c1", "source_id": "src", "text": "测量前静坐五分钟再测量。"},
        {"id": "c2", "source_id": "other", "text": "测量前静坐五分钟。"},
    ]
    result = match_anchor(anchor, chunks)
    assert result["chunk_id"] == "c1"
    assert result["method"] == "exact_normalized_quote"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_evaluation_anchors.py -q`

Expected: FAIL because anchor helpers do not exist.

- [ ] **Step 3: Implement normalization and matching**

Normalize Unicode whitespace, punctuation variants, and repeated spaces without changing Chinese characters or digits. Hash the normalized tuple `(source_id, heading_path, quote)` with SHA-256 and encode the first 16 hex characters in the anchor ID. Match only chunks with the same `source_id`; first compare normalized quote containment, then use token overlap for a deterministic score. Return one match with `chunk_id`, `score`, and `method`; raise `ValueError` for zero matches, multiple equal best matches, source mismatch, or score below `min_score`.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_evaluation_anchors.py -q`

Expected: PASS, including source hash and ambiguity cases.

- [ ] **Step 5: Commit**

```powershell
git add src/medical_rag/evaluation_anchors.py tests/test_evaluation_anchors.py
git commit -m "feat: add stable source anchors for evaluation chunks"
```

### Task 3: Add validation, remapping, and judge-export command tools

**Files:**
- Create: `src/medical_rag/evaluation_tools.py`
- Create: `data/evaluations/medical-health-v1/tools/validate_dataset.py`
- Create: `data/evaluations/medical-health-v1/tools/remap_chunk_ids.py`
- Create: `data/evaluations/medical-health-v1/tools/export_for_judge.py`
- Create: `tests/test_evaluation_tools.py`

**Interfaces:**
- `validate_release(dataset_path: Path, manifest_path: Path, registry_path: Path) -> dict`
- `remap_release(dataset_path: Path, chunks_path: Path, registry_path: Path, output_path: Path) -> dict`
- `export_judge_cases(report_path: Path, dataset_path: Path, output_path: Path) -> int`

- [ ] **Step 1: Write failing tests**

```python
def valid_row(identifier):
    return {
        "id": identifier, "question": "问题", "reference_answer": "答案",
        "key_points": ["要点"], "answerability": "answerable",
        "category": "基层高血压管理", "question_type": "procedure",
        "difficulty": "medium", "source_refs": [{
            "source_id": "src", "locator": {"page": 1},
            "quote": "原文", "anchor_id": "src#q"
        }], "relevant_chunk_ids": []
    }


def test_validate_release_enforces_count_and_quotas(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    manifest = tmp_path / "manifest.json"
    registry = tmp_path / "source-registry.json"
    dataset.write_text(json.dumps(valid_row("MHE-0001"), ensure_ascii=False) + "\n", encoding="utf-8")
    manifest.write_text(json.dumps({"question_count": 1, "quotas": {
        "category": {"基层高血压管理": 1}, "question_type": {"procedure": 1},
        "difficulty": {"medium": 1}, "answerability": {"answerable": 1}
    }}, ensure_ascii=False), encoding="utf-8")
    registry.write_text(json.dumps({"sources": [{"source_id": "src"}]}), encoding="utf-8")
    result = validate_release(dataset, manifest, registry)
    assert result["question_count"] == 1


def test_remap_fails_on_ambiguous_anchor(tmp_path):
    dataset_path = tmp_path / "questions.jsonl"
    chunks_path = tmp_path / "chunks.json"
    registry_path = tmp_path / "source-registry.json"
    dataset_path.write_text(json.dumps(valid_row("MHE-0001"), ensure_ascii=False) + "\n", encoding="utf-8")
    chunks_path.write_text(json.dumps([
        {"id": "c1", "source_id": "src", "text": "原文"},
        {"id": "c2", "source_id": "src", "text": "原文"}
    ], ensure_ascii=False), encoding="utf-8")
    registry_path.write_text(json.dumps({"sources": [{"source_id": "src"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        remap_release(dataset_path, chunks_path,
                      registry_path, tmp_path / "out.jsonl")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_evaluation_tools.py -q`

Expected: FAIL because release tooling is not implemented.

- [ ] **Step 3: Implement reusable commands**

`validate_release` loads the entire file before returning, checks the exact question count, every manifest quota, all source IDs, duplicate normalized questions, and SHA-256 entries. `remap_release` reads a deterministic JSON chunk inventory with `id`, `source_id`, `text`, `page`, and `section`; it writes a UTF-8 JSONL runtime copy with resolved `relevant_chunk_ids` and a sidecar mapping report. `export_judge_cases` produces one JSON object per evaluated case containing only the agreed judge payload and the rubric version; it omits secrets and raw provider errors.

The three thin scripts parse explicit paths with `argparse`, return exit code 2 for validation/mapping errors, and print only safe summaries. They must be runnable from the repository root with `python data/evaluations/medical-health-v1/tools/<name>.py ...`.

- [ ] **Step 4: Run focused tests and smoke commands**

Run: `python -m pytest tests/test_evaluation_tools.py -q`

Expected: PASS. Then run `python data/evaluations/medical-health-v1/tools/validate_dataset.py --help` and confirm exit code 0.

- [ ] **Step 5: Commit**

```powershell
git add src/medical_rag/evaluation_tools.py data/evaluations/medical-health-v1/tools tests/test_evaluation_tools.py
git commit -m "feat: validate remap and export evaluation releases"
```

### Task 4: Create the 500-question release and rubric artifacts

**Files:**
- Create: `data/evaluations/medical-health-v1/manifest.json`
- Create: `data/evaluations/medical-health-v1/questions.jsonl`
- Create: `data/evaluations/medical-health-v1/source-registry.json`
- Create: `data/evaluations/medical-health-v1/rubric.md`
- Create: `data/evaluations/medical-health-v1/README.md`
- Create: `data/evaluations/medical-health-v1/CHANGELOG.md`

**Interfaces:** The release passes `validate_release` and contains exactly the schema from Task 1. The source registry references every file in `data/` used by a question. The rubric has the exact 0–4 anchors, weights, abstention rules, and median/agreement calculation defined in the design document.

- [ ] **Step 1: Build the source registry**

Compute SHA-256 for each of the seven registered source files and record filename, publisher, publication year/date when present, source URL from `data/README.md`, extraction note, and a stable `source_id`. Extract headings/page text with the existing pypdf ingestion path and keep the source hash in the manifest.

- [ ] **Step 2: Author the 500 rows by quota**

Create 480 answerable rows and 20 insufficient-evidence rows. Allocate rows exactly according to the topic, question-type, and difficulty tables in the manifest. Each row must include a concise Chinese reference answer, 2–6 independently checkable key points, and one or more verbatim source quotes with page/heading anchors. Avoid questions requiring current clinical policy beyond the registered publication versions, patient-specific prescriptions, or facts absent from the source corpus.

- [ ] **Step 3: Run automated and manual checks**

Run the release validator, then inspect every row with a generated review table containing ID, question, answer, key points, source quote, and quota labels. Correct duplicates, unsupported claims, inconsistent numeric units, and missing safety qualifiers before release. Record the final dataset and rubric hashes in `manifest.json`.

- [ ] **Step 4: Write release documentation**

Document the exact command sequence for validation, remapping after re-ingestion, running the existing evaluator, exporting judge payloads, interpreting the separate retrieval/answer/safety scores, and handling version changes. State that this is not a clinical validation set and that reports may contain sensitive question/evidence text.

- [ ] **Step 5: Commit the release**

```powershell
git add data/evaluations/medical-health-v1
git commit -m "feat: add medical health evaluation dataset v1"
```

### Task 5: Integrate CLI/report metadata and verify end to end

**Files:**
- Modify: `src/medical_rag/evaluate.py`
- Modify: `tests/test_evaluate_cli.py`
- Modify: `README.md`

**Interfaces:** `python -m medical_rag.evaluate --dataset <runtime-jsonl> --output <report.json> --top-k 5 ...` accepts the remapped release copy and writes dataset version, rubric version, answerability counts, and separate retrieval/answer/safety summaries.

- [ ] **Step 1: Write failing CLI tests**

Add tests that pass a rich remapped dataset to the injected factory, assert the factory is not called when validation fails, assert `insufficient_evidence` rows do not invoke semantic judges, and assert the JSON report contains `dataset_version`, `rubric_version`, `abstention_correct_count`, and `unsupported_answer_count`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_evaluate_cli.py -q`

Expected: FAIL because the CLI/report does not yet expose release metadata and abstention summaries.

- [ ] **Step 3: Implement CLI integration**

Add an optional `--manifest` argument defaulting to the sibling manifest when the dataset is under the release directory. Load and validate the dataset before constructing Settings, Milvus, or model transports. Pass the release metadata through `evaluate_cases`, serialize with `allow_nan=False`, and preserve the existing safe error messages and provider choices.

- [ ] **Step 4: Update README and run the full suite**

Document the release workflow, exact formulas, the difference between source JSONL and remapped runtime JSONL, and the three-platform judge protocol. Run:

```powershell
python -m pytest -q --cov=medical_rag --cov-report=term-missing
python -m compileall -q src
git diff --check
python data/evaluations/medical-health-v1/tools/validate_dataset.py --dataset data/evaluations/medical-health-v1/questions.jsonl --manifest data/evaluations/medical-health-v1/manifest.json --registry data/evaluations/medical-health-v1/source-registry.json
```

Expected: all tests pass, compilation succeeds, diff check is clean, and release validation reports 500 valid questions with the exact quotas.

- [ ] **Step 5: Commit**

```powershell
git add src/medical_rag/evaluate.py tests/test_evaluate_cli.py README.md
git commit -m "feat: integrate versioned medical evaluation release"
```

## Self-review

- The design requirements map to Tasks 1–5: rich schema and abstention semantics (Task 1), stable anchors and hashes (Task 2), operational tooling (Task 3), the complete 500-row release and rubric (Task 4), and CLI/report integration (Task 5).
- Every task has exact files, interfaces, tests, commands, and commit boundaries.
- The quota totals are internally consistent: 110+100+75+75+70+50+20=500; 140+120+70+100+40+30=500; 180+230+90=500; 480+20=500.
- No task relies on silently guessing unresolved source mappings or on paid model calls in tests.
