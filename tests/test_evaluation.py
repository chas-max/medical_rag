import json

import pytest

from medical_rag.evaluation import evaluate_cases, load_dataset
from medical_rag.types import Chunk, Evidence


def evidence(block_id):
    return Evidence(Chunk(block_id, "doc", "source", f"证据 {block_id}", "guide.md", "", None, 0), 0.9)


class Generator:
    def __init__(self, abstained=False):
        self.calls = []
        self.abstained = abstained

    def answer(self, question, items, provider):
        self.calls.append((question, [x.chunk.id for x in items], provider))
        return {"answer": "资料不足" if self.abstained else "回答", "citations": [],
                "abstained": self.abstained}


class Service:
    def __init__(self, ids=("a", "b"), abstained=False):
        self.ids = ids
        self.generator = Generator(abstained)
        self.calls = []

    def search(self, question, k):
        self.calls.append((question, k))
        return [evidence(i) for i in self.ids]


class Judge:
    model = "judge-model"

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def score_faithfulness(self, question, answer, contexts):
        self.calls.append("faithfulness")
        return {"score": 1.0, "reason": "支持", "model": self.model}

    def score_context(self, question, context):
        self.calls.append("context")
        if self.fail and self.fail in context:
            raise ValueError("private-key=never-persist")
        return {"score": 0.5, "reason": "部分相关", "model": self.model}

    def score_answer(self, question, answer):
        self.calls.append("answer")
        return {"score": 0.75, "reason": "回应", "model": self.model}


def test_dataset_validated_before_use(tmp_path):
    path = tmp_path / "dataset.jsonl"
    path.write_text('\n'.join([
        json.dumps({"question": "问题", "relevant_chunk_ids": ["a"], "id": "first"}),
        json.dumps({"question": "另一个", "relevant_chunk_ids": ["b"]}),
    ]), encoding="utf-8")
    cases = load_dataset(path)
    assert [c["id"] for c in cases] == ["first", "2"]


@pytest.mark.parametrize("rows", [
    "", "{}", "not json", '{"question":" ","relevant_chunk_ids":["a"]}',
    '{"question":"q","relevant_chunk_ids":[]}',
    '{"question":"q","relevant_chunk_ids":["a","a"]}',
    '{"question":"q","relevant_chunk_ids":[1]}',
    '{"question":"q","relevant_chunk_ids":["a"],"unexpected":1}',
    '{"question":"q","relevant_chunk_ids":["a"],"id":"z"}\n'
    '{"question":"q","relevant_chunk_ids":["a"],"id":"z"}',
])
def test_invalid_dataset_rejected_with_line_number(tmp_path, rows):
    path = tmp_path / "bad.jsonl"
    path.write_text(rows, encoding="utf-8")
    with pytest.raises(ValueError, match="行|空"):
        load_dataset(path)


def test_report_reuses_evidence_and_aggregates():
    service, judge = Service(), Judge()
    report = evaluate_cases([{"id": "1", "question": "问题", "relevant_chunk_ids": ["b"]}],
                            service, judge, 2, "deepseek", "qwen")
    case = report["cases"][0]
    assert service.calls == [("问题", 2)]
    assert service.generator.calls == [("问题", ["a", "b"], "deepseek")]
    assert case["retrieval"]["rr"] == 0.5
    assert case["retrieval"]["ap"] == 0.5
    assert case["faithfulness"]["score"] == 1
    assert case["context_relevance"]["score"] == 0.5
    assert len(case["context_relevance"]["items"]) == 2
    assert report["summary"]["map"] == 0.5
    assert report["summary"]["mrr"] == 0.5
    assert report["summary"]["answer_relevance"] == 0.75
    assert report["summary"]["valid_counts"]["faithfulness"] == 1


def test_abstention_keeps_retrieval_and_skips_judge():
    service, judge = Service(ids=(), abstained=True), Judge()
    report = evaluate_cases([{"id": "1", "question": "q", "relevant_chunk_ids": ["a"]}],
                            service, judge, 5, "qwen", "deepseek")
    assert not judge.calls
    assert report["cases"][0]["faithfulness"] is None
    assert report["summary"]["faithfulness"] is None
    assert report["summary"]["abstention_rate"] == 1
    assert report["summary"]["recall"] == 0


def test_partial_judge_failure_keeps_other_dimensions_and_hides_error():
    report = evaluate_cases([{"id": "1", "question": "q", "relevant_chunk_ids": ["a"]}],
                            Service(), Judge(fail="b"), 2, "qwen", "deepseek")
    case = report["cases"][0]
    assert case["context_relevance"]["score"] is None
    assert case["faithfulness"]["score"] == 1
    assert case["answer_relevance"]["score"] == 0.75
    assert report["summary"]["judge_failures"] == 1
    assert "private-key" not in json.dumps(report)


def test_answer_and_search_failure_do_not_hide_other_cases():
    class Broken(Service):
        def search(self, question, k):
            if question == "broken":
                raise RuntimeError("secret search response")
            return super().search(question, k)

    service = Broken()
    report = evaluate_cases([
        {"id": "1", "question": "broken", "relevant_chunk_ids": ["a"]},
        {"id": "2", "question": "working", "relevant_chunk_ids": ["b"]},
    ], service, Judge(), 2, "qwen", "deepseek")
    assert report["cases"][0]["error"] == "SEARCH_FAILED"
    assert report["summary"]["retrieval_valid_count"] == 1
    assert report["summary"]["mrr"] == 0.5
    assert "secret" not in json.dumps(report)


def test_answer_failure_preserves_retrieval_without_judging():
    service, judge = Service(), Judge()

    def broken_answer(*args):
        raise RuntimeError("secret provider message")

    service.generator.answer = broken_answer
    report = evaluate_cases([{"id": "1", "question": "q", "relevant_chunk_ids": ["a"]}],
                            service, judge, 2, "qwen", "deepseek")
    case = report["cases"][0]
    assert case["retrieval"]["recall"] == 1
    assert case["error"] == "ANSWER_FAILED"
    assert case["faithfulness"] is None
    assert not judge.calls
    assert "secret" not in json.dumps(report)


def test_insufficient_evidence_records_abstention_and_excludes_recall():
    case = {
        "id": "MHE-1", "question": "q", "relevant_chunk_ids": [],
        "reference_answer": "资料不足时拒答", "key_points": ["拒答"],
        "answerability": "insufficient_evidence", "category": "公卫",
        "question_type": "fact", "difficulty": "easy",
        "source_refs": [{"source_id": "s", "locator": {"heading": "h"},
                         "quote": "无足够证据", "anchor_id": "s#h#1"}],
    }
    service, judge = Service(ids=(), abstained=True), Judge()
    report = evaluate_cases([case], service, judge, 2, "qwen", "deepseek")
    result = report["cases"][0]
    assert result["expected_abstention"] is True
    assert result["correct_abstention"] is True
    assert result["unsupported_answer"] is False
    assert report["summary"]["expected_abstention_count"] == 1
    assert report["summary"]["correct_abstention_rate"] == 1
    assert report["summary"]["recall"] is None
    assert not judge.calls
