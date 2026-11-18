# 医疗健康评测集 v2（150 题）与分级多证据评测系统实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建覆盖 `data/` 下全部 35 份语料的 150 题医疗健康评测集 `data/evaluations/medical-health-v2/`（120 可回答 + 30 安全拒答），并向后兼容升级 `src/medical_rag/` 评测模块以支持分级相关性（`relevance` 0/1/2）、`NDCG@k`、`Core-Recall@k`、`Hit@k` 与干扰命中拒答率。

**Architecture:** 在保留 `medical-health-v1` 及原有二元评测接口完全兼容的基础上，依次升级指标层（`evaluation_metrics.py`）、数据契约校验层（`evaluation_dataset.py`）、校验与重映射层（`evaluation_tools.py`）和报告/CLI 层（`evaluation_report.py`、`evaluate.py`），随后基于 `data/` 全 35 份原始文档抽取真实页码/标题原文，编写并校验通过 150 题 `medical-health-v2` 发布集及集成测试。

**Tech Stack:** Python 3.11, pytest, pypdf, hashlib, json, `medical_rag.ingest` / `medical_rag.evaluation_*`

## Global Constraints

- 数据集路径：`data/evaluations/medical-health-v2/`，与 `data/evaluations/medical-health-v1/` 完全隔离，不改动 v1 任何文件。
- 全量语料：`data/` 下全部 35 份公开文件（1 份 `.md` + 34 份 `.pdf`），每份文件至少被 ≥3 条 `source_refs` 命中。
- 150 题固定配额：
  - `answerability`: `{"answerable": 120, "insufficient_evidence": 30}`
  - `category`: `{"公共卫生与健康素养": 16, "心血管诊疗与食养": 20, "内分泌代谢与体重管理": 26, "呼吸、传染病与儿科": 18, "恶性肿瘤规范诊疗": 16, "精神神经与老年/肾脏健康": 20, "膳食营养基础准则": 10, "跨主题综合与多版本对比": 24}`
  - `question_type`: `{"multi_evidence": 40, "comparison": 24, "scenario": 42, "procedure": 18, "numeric": 16, "fact": 10}`
  - `difficulty`: `{"easy": 25, "medium": 70, "hard": 55}`
- 分级相关性：`relevance` 取值 `2`（`core`，缺省值）、`1`（`supplementary`）、`0`（`context_only`，仅用于拒答题近邻干扰）。
- 反幻觉红线：所有 `quote`、`locator`、`anchor_id` 必须 100% 从 `data/` 源文件抽取并通过 `validate_release` 与默认分块 `remap_release` 实测。

---

### Task 1: 升级 `evaluation_metrics.py` 支持分级指标（`ndcg`、`core_recall`、`hit_rate`）

**Files:**
- Modify: `src/medical_rag/evaluation_metrics.py:1-32`
- Test: `tests/test_evaluation_metrics.py`

**Interfaces:**
- Consumes: `ranked_ids: list[str]`, `relevant_ids: set[str]`, `k: int`, optional `chunk_grades: dict[str, int] | None = None`
- Produces: `retrieval_scores(...) -> dict[str, Any]` returning existing keys (`precision`, `recall`, `f1`, `ap`, `rr`, `retrieved_count`, `hit_ids`) plus `ndcg: float`, `core_recall: float`, `hit_rate: float`.

- [ ] **Step 1: Write the failing test**

Add tests to `tests/test_evaluation_metrics.py`:

```python
def test_graded_metrics_reward_core_chunks_and_top_ranks():
    grades = {"core1": 2, "core2": 2, "supp1": 1}
    perfect = retrieval_scores(
        ["core1", "core2", "supp1", "x"],
        set(grades),
        4,
        chunk_grades=grades,
    )
    assert perfect["ndcg"] == pytest.approx(1.0)
    assert perfect["core_recall"] == pytest.approx(1.0)
    assert perfect["hit_rate"] == 1.0

    # Putting supplementary chunk ahead of missed core2 lowers NDCG and core_recall
    suboptimal = retrieval_scores(
        ["supp1", "core1", "x"],
        set(grades),
        3,
        chunk_grades=grades,
    )
    assert suboptimal["recall"] == pytest.approx(2 / 3)
    assert suboptimal["core_recall"] == pytest.approx(0.5)
    assert 0 < suboptimal["ndcg"] < 1.0
    assert suboptimal["hit_rate"] == 1.0


def test_graded_metrics_fallback_safely_without_grades_or_hits():
    score = retrieval_scores(["x", "y"], {"a", "b"}, 2)
    assert score["ndcg"] == 0.0
    assert score["core_recall"] == 0.0
    assert score["hit_rate"] == 0.0

    only_supp = retrieval_scores(["s1"], {"s1", "s2"}, 2, chunk_grades={"s1": 1, "s2": 1})
    assert only_supp["core_recall"] == pytest.approx(0.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `d:\medical-rag-resume\.venv\Scripts\python.exe -m pytest tests/test_evaluation_metrics.py -v`
Expected: FAIL with `TypeError: retrieval_scores() got an unexpected keyword argument 'chunk_grades'`

- [ ] **Step 3: Write minimal implementation in `src/medical_rag/evaluation_metrics.py`**

```python
"""Deterministic retrieval metrics over ranked, labeled chunk IDs."""

from math import isfinite, log2
from typing import Any


def _graded_ndcg(ranked: list[str], relevant_ids: set[str], grades: dict[str, int], k: int) -> float:
    dcg = sum(
        ((2 ** grades.get(block_id, 2)) - 1) / log2(pos + 1)
        for pos, block_id in enumerate(ranked, 1)
        if block_id in relevant_ids
    )
    ideal_gains = sorted(
        ((2 ** grades.get(cid, 2)) - 1 for cid in relevant_ids),
        reverse=True,
    )[:k]
    idcg = sum(gain / log2(pos + 1) for pos, gain in enumerate(ideal_gains, 1))
    return dcg / idcg if idcg > 0 else 0.0


def retrieval_scores(
    ranked_ids: list[str],
    relevant_ids: set[str],
    k: int,
    *,
    chunk_grades: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Score the first k unique retrieved IDs against binary or graded labels."""
    if k < 1 or not relevant_ids:
        raise ValueError("k and relevant IDs must be nonempty")
    ranked = list(dict.fromkeys(ranked_ids))[:k]
    grades = {cid: int(chunk_grades.get(cid, 2)) for cid in relevant_ids} if chunk_grades else {}
    hits = []
    precision_sum = 0.0
    reciprocal_rank = 0.0
    for position, block_id in enumerate(ranked, 1):
        if block_id in relevant_ids:
            hits.append(block_id)
            precision_sum += len(hits) / position
            if not reciprocal_rank:
                reciprocal_rank = 1 / position
    precision = len(hits) / len(ranked) if ranked else 0.0
    recall = len(hits) / len(relevant_ids)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    core_ids = {cid for cid in relevant_ids if grades.get(cid, 2) >= 2} or relevant_ids
    core_hits = sum(1 for block_id in hits if block_id in core_ids)
    core_recall = core_hits / len(core_ids)
    ndcg = _graded_ndcg(ranked, relevant_ids, grades, k)
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "ap": precision_sum / min(len(relevant_ids), k),
        "rr": reciprocal_rank,
        "ndcg": ndcg,
        "core_recall": core_recall,
        "hit_rate": 1.0 if hits else 0.0,
        "retrieved_count": len(ranked),
        "hit_ids": hits,
    }


def mean_or_none(values: list[float]) -> float | None:
    """Average valid scores; signal an empty sample explicitly."""
    if any(not isfinite(value) for value in values):
        raise ValueError("scores must be finite")
    return sum(values) / len(values) if values else None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `d:\medical-rag-resume\.venv\Scripts\python.exe -m pytest tests/test_evaluation_metrics.py -v`
Expected: PASS

---

### Task 2: 升级 `evaluation_dataset.py` 支持分级 `relevance`、`chunk_grades`、`context_chunk_ids` 与拒答题零正例/干扰引用

**Files:**
- Modify: `src/medical_rag/evaluation_dataset.py:16-113`
- Test: `tests/test_evaluation_dataset.py`

**Interfaces:**
- Consumes: rich dataset row dict
- Produces: validated normalized row dict accepting optional `source_refs[].relevance` in `{0, 1, 2}`, optional `chunk_grades: dict[str, int]` (values `1` or `2`), optional `context_chunk_ids: list[str]`, and `source_refs == []` or `relevance == 0`-only refs on `insufficient_evidence`.

- [ ] **Step 1: Write the failing tests**

Add tests to `tests/test_evaluation_dataset.py`:

```python
def test_graded_relevance_and_insufficient_context_only_refs():
    answerable = rich_row(source_refs=[
        {"source_id": "g1", "locator": {"page": 1}, "quote": "核心句", "anchor_id": "g1#p1#1", "relevance": 2},
        {"source_id": "g2", "locator": {"page": 2}, "quote": "补充句", "anchor_id": "g2#p2#2", "relevance": 1},
    ], chunk_grades={"chunk-1": 2})
    assert validate_evaluation_row(answerable, 1)["chunk_grades"] == {"chunk-1": 2}

    insuff_empty = rich_row(answerability="insufficient_evidence", source_refs=[], relevant_chunk_ids=[])
    assert validate_evaluation_row(insuff_empty, 1)["source_refs"] == []

    insuff_decoy = rich_row(
        answerability="insufficient_evidence",
        source_refs=[{"source_id": "g1", "locator": {"page": 1}, "quote": "干扰句", "anchor_id": "g1#p1#0", "relevance": 0}],
        relevant_chunk_ids=[],
        context_chunk_ids=["decoy-1"],
    )
    assert validate_evaluation_row(insuff_decoy, 1)["context_chunk_ids"] == ["decoy-1"]


@pytest.mark.parametrize("overrides", [
    {"source_refs": [{"source_id": "g1", "locator": {"page": 1}, "quote": "q", "anchor_id": "a", "relevance": 3}]},
    {"source_refs": [{"source_id": "g1", "locator": {"page": 1}, "quote": "q", "anchor_id": "a", "relevance": True}]},
    {"source_refs": [{"source_id": "g1", "locator": {"page": 1}, "quote": "q", "anchor_id": "a", "relevance": 0}]},
    {"answerability": "insufficient_evidence",
     "source_refs": [{"source_id": "g1", "locator": {"page": 1}, "quote": "q", "anchor_id": "a", "relevance": 2}]},
    {"chunk_grades": {"chunk-1": 0}},
    {"context_chunk_ids": ["d1", "d1"]},
])
def test_invalid_graded_fields_are_rejected(overrides):
    with pytest.raises(ValueError):
        validate_evaluation_row(rich_row(**overrides), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `d:\medical-rag-resume\.venv\Scripts\python.exe -m pytest tests/test_evaluation_dataset.py -v`
Expected: FAIL (because `insufficient_evidence` with `source_refs=[]` currently raises ValueError and invalid `relevance` / `chunk_grades` / `context_chunk_ids` are not checked)

- [ ] **Step 3: Write minimal implementation in `src/medical_rag/evaluation_dataset.py`**

Update `validate_evaluation_row` in `src/medical_rag/evaluation_dataset.py` (keeping existing behavior for legacy and v1 rows intact):

```python
VALID_RELEVANCES = {0, 1, 2}


def _validate_chunk_list(values: Any, line_number: int, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(values, list) or (not values and not allow_empty):
        raise ValueError(f"第 {line_number} 行题目或相关块 ID 无效")
    if any(not _nonempty(item) for item in values) or len(values) != len(set(values)):
        raise ValueError(f"第 {line_number} 行题目或相关块 ID 无效")
    return values
```

Inside `validate_evaluation_row`:
- Validate `refs`:
  - `if not isinstance(refs, list): raise ValueError(...)`
  - For `answerable`: `if not refs: raise ValueError(...)`
  - For each `ref`: validate `source_id`, `quote`, `locator`, `anchor_id`, and if `"relevance" in ref`: require `type(ref["relevance"]) is int and ref["relevance"] in VALID_RELEVANCES`.
  - If `row["answerability"] == "answerable"` and all refs declare `"relevance"`, ensure at least one has `ref.get("relevance", 2) >= 1` (if any ref has `relevance`, require at least one with `>= 1`; note v1 omits `relevance` so defaults to `2`).
  - If `row["answerability"] == "insufficient_evidence"`: any ref that explicitly specifies `"relevance"` must have `ref["relevance"] == 0` (v1 legacy rows without `"relevance"` still pass!).
- If `"chunk_grades" in row`: require `isinstance(row["chunk_grades"], dict)` with nonempty str keys and `type(v) is int and v in {1, 2}`.
- If `"context_chunk_ids" in row`: `_validate_chunk_list(row["context_chunk_ids"], line_number, allow_empty=True)`.

- [ ] **Step 4: Run tests to verify pass**

Run: `d:\medical-rag-resume\.venv\Scripts\python.exe -m pytest tests/test_evaluation_dataset.py -v`
Expected: PASS

---

### Task 3: 升级 `evaluation_tools.py` 支持分级重映射（`chunk_grades`、`context_chunk_ids`）与零锚点校验

**Files:**
- Modify: `src/medical_rag/evaluation_tools.py:152-166,235-258`
- Test: `tests/test_evaluation_tools.py`

**Interfaces:**
- Consumes: `dataset_path`, `chunks_path`, `registry_path`, `output_path`
- Produces: `remap_release()` populates `relevant_chunk_ids` (`relevance >= 1`), `chunk_grades` (max grade per chunk when `relevance` is used or `grade >= 1`), and `context_chunk_ids` (`relevance == 0` on `insufficient_evidence`, marked `"context_only"` in `.mapping.json`); `validate_release()` verifies all present `source_refs` including `relevance == 0`.

- [ ] **Step 1: Write the failing tests**

Add tests to `tests/test_evaluation_tools.py`:

```python
def test_remap_builds_chunk_grades_and_context_chunk_ids(tmp_path):
    from medical_rag.evaluation_anchors import make_anchor_id

    source = tmp_path / "guide.md"
    source.write_text("## 章节一\n核心证据A。\n补充证据B。\n干扰背景C。\n", encoding="utf-8")
    digest = source_sha256(source)
    registry = tmp_path / "source-registry.json"
    write_json(registry, {"sources": [{"source_id": "src", "filename": source.name, "sha256": digest}]})
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"question_count": 2})

    dataset = tmp_path / "questions.jsonl"
    ans_row = valid_row(
        "MHE2-0001",
        question="可回答问题",
        source_refs=[
            {"source_id": "src", "locator": {"heading": "章节一"}, "quote": "核心证据A。",
             "anchor_id": make_anchor_id("src", ["章节一"], "核心证据A。"), "relevance": 2},
            {"source_id": "src", "locator": {"heading": "章节一"}, "quote": "补充证据B。",
             "anchor_id": make_anchor_id("src", ["章节一"], "补充证据B。"), "relevance": 1},
        ],
    )
    ins_row = valid_row(
        "MHE2-0002",
        question="拒答问题",
        answerability="insufficient_evidence",
        source_refs=[
            {"source_id": "src", "locator": {"heading": "章节一"}, "quote": "干扰背景C。",
             "anchor_id": make_anchor_id("src", ["章节一"], "干扰背景C。"), "relevance": 0},
        ],
    )
    dataset.write_text(
        json.dumps(ans_row, ensure_ascii=False) + "\n" + json.dumps(ins_row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert validate_release(dataset, manifest, registry)["question_count"] == 2

    chunks = tmp_path / "chunks.json"
    write_json(chunks, [
        {"id": "c1", "source_id": "src", "text": "核心证据A。补充证据B。", "page": 0, "section": "章节一", "sha256": digest},
        {"id": "c2", "source_id": "src", "text": "补充证据B。", "page": 0, "section": "章节一", "sha256": digest},
        {"id": "c3", "source_id": "src", "text": "干扰背景C。", "page": 0, "section": "章节一", "sha256": digest},
    ])
    output = tmp_path / "runtime.jsonl"
    remap_release(dataset, chunks, registry, output)
    runtime_rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert runtime_rows[0]["relevant_chunk_ids"] == ["c1", "c2"]
    assert runtime_rows[0]["chunk_grades"] == {"c1": 2, "c2": 1}
    assert runtime_rows[1]["relevant_chunk_ids"] == []
    assert runtime_rows[1]["context_chunk_ids"] == ["c3"]
    sidecar = json.loads(output.with_suffix(".jsonl.mapping.json").read_text(encoding="utf-8"))
    assert any(item["method"] == "context_only" and item["chunk_id"] == "c3" for item in sidecar)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `d:\medical-rag-resume\.venv\Scripts\python.exe -m pytest tests/test_evaluation_tools.py -v`
Expected: FAIL (`KeyError: 'chunk_grades'` / `'context_chunk_ids'`)

- [ ] **Step 3: Update `remap_release` in `src/medical_rag/evaluation_tools.py`**

In `remap_release`:
- Track `grades: dict[str, int] = {}` and `context_ids: list[str] = []` per row.
- If `case.get("answerability") == "insufficient_evidence"`:
  - If `ref.get("relevance") == 0`: call `match_anchor(ref, chunks)`, append unique `chunk_id`s to `context_ids`, and record `method: "context_only"` in `mapping_report`.
  - Otherwise (legacy v1 without `relevance == 0`): keep `method: "skipped"`.
- If `answerability == "answerable"`:
  - `grade = int(ref.get("relevance", 2))`
  - Match anchor, update `grades[chunk_id] = max(grades.get(chunk_id, 0), grade)`, append unique `chunk_id` to `resolved`, record `grade` in `mapping_report` entry (optional key or keep compatible).
- Set `row["relevant_chunk_ids"] = resolved`; if any ref used `"relevance"` (or `grades` has entries), set `row["chunk_grades"] = grades` (actually setting `row["chunk_grades"] = grades` whenever `grades` is non-empty is clean and compatible; check `test_evaluation_tools.py` to confirm no existing test asserts exact keys of runtime row!). Also if `context_ids`: set `row["context_chunk_ids"] = context_ids` (or for `insufficient_evidence` when `any("relevance" in r for r in case["source_refs"])` or `not case["source_refs"]`, set `row["context_chunk_ids"] = context_ids` cleanly).

- [ ] **Step 4: Run tests to verify pass**

Run: `d:\medical-rag-resume\.venv\Scripts\python.exe -m pytest tests/test_evaluation_tools.py -v`
Expected: PASS

---

### Task 4: 升级 `evaluation_report.py` 与 `evaluate.py` 汇总分级指标与干扰拒答率

**Files:**
- Modify: `src/medical_rag/evaluation_report.py:6-101`
- Modify: `src/medical_rag/evaluate.py:87-94`
- Test: `tests/test_evaluation.py`, `tests/test_evaluate_cli.py`

**Interfaces:**
- Consumes: `case` with optional `chunk_grades` & `context_chunk_ids`
- Produces: `summary` includes `ndcg`, `core_recall`, `hit_rate`, `decoy_abstention_count`, `decoy_abstention_rate`; `evaluate.py` ignores `{"relevant_chunk_ids", "chunk_grades", "context_chunk_ids"}` during release-vs-runtime consistency diff.

- [ ] **Step 1: Write failing tests in `tests/test_evaluation.py` and `tests/test_evaluate_cli.py`**

In `tests/test_evaluation.py`:
```python
def test_report_aggregates_graded_retrieval_and_decoy_abstention():
    service = Service(ids=("supp", "core"), abstained=True)
    judge = Judge()
    cases = [
        {"id": "1", "question": "q1", "relevant_chunk_ids": ["core", "supp"],
         "chunk_grades": {"core": 2, "supp": 1}},
        {"id": "2", "question": "q2", "relevant_chunk_ids": [],
         "answerability": "insufficient_evidence", "context_chunk_ids": ["supp"]},
    ]
    report = evaluate_cases(cases, service, judge, 2, "qwen", "qwen")
    assert report["summary"]["core_recall"] == pytest.approx(1.0)
    assert 0 < report["summary"]["ndcg"] < 1.0
    assert report["summary"]["hit_rate"] == pytest.approx(1.0)
    assert report["cases"][1]["context_hit_count"] == 1
    assert report["summary"]["decoy_abstention_count"] == 1
    assert report["summary"]["decoy_abstention_rate"] == pytest.approx(1.0)
```

In `tests/test_evaluate_cli.py`:
Add test verifying CLI `--manifest` accepts runtime rows with `chunk_grades` and `context_chunk_ids` derived from `questions.jsonl`.

- [ ] **Step 2: Run tests to verify failure**

Run: `d:\medical-rag-resume\.venv\Scripts\python.exe -m pytest tests/test_evaluation.py tests/test_evaluate_cli.py -v`
Expected: FAIL (`KeyError: 'core_recall'` / `KeyError: 'decoy_abstention_rate'`)

- [ ] **Step 3: Implement minimal updates in `evaluation_report.py` and `evaluate.py`**

- In `evaluation_report.py`:
  - `new_case`: copy `"chunk_grades"`, `"context_chunk_ids"` if present in `case`.
  - `score_evidence`: pass `chunk_grades=result.get("chunk_grades")` into `retrieval_scores(...)`; if `result.get("expected_abstention")`: compute `context_ids = set(result.get("context_chunk_ids") or ())`, record `result["context_hit_count"] = sum(1 for item in evidence if item.chunk.id in context_ids)`.
  - `build_report`: aggregate `"ndcg"`, `"core_recall"`, `"hit_rate"` using `mean_or_none` over `valid_retrieval`; compute `decoy_cases = [item for item in abstention_cases if (item.get("context_hit_count") or 0) > 0 and item["correct_abstention"] is not None]`, set `decoy_abstention_count = sum(item["correct_abstention"] for item in decoy_cases)` and `decoy_abstention_rate = mean_or_none([item["correct_abstention"] for item in decoy_cases])`.
- In `evaluate.py`:
  - Define `_DERIVED_RUNTIME_FIELDS = {"relevant_chunk_ids", "chunk_grades", "context_chunk_ids"}` and strip those keys when comparing `runtime` vs `original` rows.

- [ ] **Step 4: Run tests to verify pass**

Run: `d:\medical-rag-resume\.venv\Scripts\python.exe -m pytest tests/test_evaluation.py tests/test_evaluate_cli.py tests/test_workflow_evaluation.py -v`
Expected: PASS

---

### Task 5: 搭建 `data/evaluations/medical-health-v2/` 基础清单、全 35 份语料注册表与文档工具

**Files:**
- Create: `data/evaluations/medical-health-v2/source-registry.json`
- Create: `data/evaluations/medical-health-v2/rubric.md`
- Create: `data/evaluations/medical-health-v2/README.md`
- Create: `data/evaluations/medical-health-v2/CHANGELOG.md`
- Create: `data/evaluations/medical-health-v2/tools/validate_dataset.py`
- Create: `data/evaluations/medical-health-v2/tools/remap_chunk_ids.py`
- Create: `data/evaluations/medical-health-v2/tools/export_for_judge.py`
- Create: `data/evaluations/medical-health-v2/tools/aggregate_judges.py`

- [ ] **Step 1: Compute exact SHA-256 of all 35 files in `data/` via Python and generate `source-registry.json`**
- [ ] **Step 2: Author `rubric.md` (v2.0, updating version identifier to `medical-health-v2.0` and clarifying `source_excerpts` handling for `insufficient_evidence` context-only decoy quotes), `README.md`, `CHANGELOG.md`, and `tools/*.py`**
- [ ] **Step 3: Verify `source-registry.json` hashes match all 35 files on disk with 0 missing**

---

### Task 6: 抽取 35 份语料原文并编制、编译与校验 150 题 `medical-health-v2` 题库

**Files:**
- Create: `data/evaluations/medical-health-v2/drafts/authoring_v2.json` (or domain batch files)
- Create: `data/evaluations/medical-health-v2/questions.jsonl` (150 lines, `MHE2-0001` ~ `MHE2-0150`)
- Create: `data/evaluations/medical-health-v2/manifest.json`

- [ ] **Step 1: Extract real page/section text index from all 35 files in `data/` into scratch workspace**
- [ ] **Step 2: Author 150 medically accurate, non-redundant questions strictly matching the spec quotas (`120 answerable` + `30 insufficient_evidence`), ensuring every answerable question has 3–6+ distinct `source_refs` (`relevance` 2/1), every single one of the 35 files is hit by ≥3 refs, and 30 natural-phrasing abstention/safety questions cover all 5 boundary categories (with `relevance: 0` context decoys on the near-miss traps)**
- [ ] **Step 3: Compile `questions.jsonl` (deterministic `anchor_id` via `make_anchor_id`) and compute exact SHA-256 digests into `manifest.json`**
- [ ] **Step 4: Run `validate_dataset.py` CLI and verify 150/150 pass with exact quota and zero duplicate questions/anchors**

Run: `d:\medical-rag-resume\.venv\Scripts\python.exe data/evaluations/medical-health-v2/tools/validate_dataset.py --dataset data/evaluations/medical-health-v2/questions.jsonl --manifest data/evaluations/medical-health-v2/manifest.json --registry data/evaluations/medical-health-v2/source-registry.json`
Expected: `validated 150 questions; sources=35`

---

### Task 7: 端到端默认分块重映射集成测试与全量回归验证

**Files:**
- Create: `tests/test_medical_health_v2_release_integration.py`

- [ ] **Step 1: Write `tests/test_medical_health_v2_release_integration.py`**
  - Verify `validate_release` passes for `medical-health-v2` (`question_count == 150`, `len(source_ids) == 35`).
  - Parse and chunk all 35 files with `parse_document` + `chunk_document(..., ConstantEmbedder())`, run `remap_release`, assert:
    - All 120 `answerable` questions map to non-empty `relevant_chunk_ids` and `chunk_grades` containing at least one grade `2`.
    - Mean `len(relevant_chunk_ids)` across `answerable` questions is `>= 3.0` (confirming multi-chunk coverage).
    - All 30 `insufficient_evidence` questions keep `relevant_chunk_ids == []`, and questions with `relevance == 0` anchors populate `context_chunk_ids`.
    - Mapping methods exclude unexpected failures (`<= {"containment", "containment_multi", "parent_containment", "parent_containment_multi", "context_only", "skipped"}`).
- [ ] **Step 2: Run v2 release integration test + all evaluation unit tests + `ruff check`**

Run:
`d:\medical-rag-resume\.venv\Scripts\python.exe -m pytest tests/test_medical_health_v2_release_integration.py tests/test_evaluation*.py tests/test_evaluate_cli.py tests/test_workflow_evaluation.py -v`
Expected: All PASS with 0 failures.
