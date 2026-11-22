import json

from medical_rag.evaluate import main as evaluate_main
from medical_rag.types import Chunk, Evidence


class Generator:
    def answer(self, question, evidence, provider):
        return {"answer": "回答", "citations": [], "abstained": False}


class Service:
    generator = Generator()

    def search(self, question, top_k):
        return [Evidence(Chunk("c1", "doc", "source", "证据", "guide.md", "", None, 0), 0.9)]


class Judge:
    model = "judge"

    def __init__(self):
        self.calls = []

    def score_faithfulness(self, question, answer, contexts):
        self.calls.append("faithfulness")
        return {"score": 0.4, "reason": "local", "model": self.model}

    def score_context(self, question, context):
        self.calls.append("context")
        return {"score": 0.4, "reason": "local", "model": self.model}

    def score_answer(self, question, answer):
        self.calls.append("answer")
        return {"score": 0.4, "reason": "local", "model": self.model}


def _block(case_id="1"):
    return {
        "enabled": True,
        "version": "0.4.4",
        "config": {"model": "qwen3.8-flash", "metrics": ["faithfulness"]},
        "summary": {"faithfulness": 0.8},
        "valid_counts": {"faithfulness": 1},
        "failure_counts": {},
        "skip_counts": {},
        "sample_count": 1,
        "scored_count": 1,
        "cases": [
            {
                "id": case_id,
                "retrieval_hit_count": 1,
                "semantic_context_count": 1,
                "scores": {"faithfulness": 0.8},
                "errors": {},
                "skipped": {},
                "warnings": [],
            }
        ],
    }


def _write_dataset(path):
    row = {"id": "1", "question": "q", "relevant_chunk_ids": ["c1"]}
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")


def test_evaluate_cli_runs_ragas_and_disables_local_judge(tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "report.json"
    raw = tmp_path / "ragas-raw.json"
    _write_dataset(dataset)
    judge = Judge()
    calls = []

    def factory(settings, answer_provider, judge_provider):
        return Service(), judge

    def ragas_factory(report, config):
        calls.append((report, config))
        return _block()

    result = evaluate_main(
        ["--dataset", str(dataset), "--output", str(output), "--ragas",
         "--ragas-metrics", "faithfulness", "--ragas-raw-output", str(raw)],
        factory=factory,
        ragas_factory=ragas_factory,
    )
    assert result == 0
    assert calls and calls[0][1].metrics == ("faithfulness",)
    assert judge.calls == []
    report = json.loads(output.read_text(encoding="utf-8"))
    assert json.loads(raw.read_text(encoding="utf-8"))["summary"]["faithfulness"] == 0.8
    assert report["config"]["ragas"]["model"] == "qwen3.8-flash"
    assert report["cases"][0]["faithfulness"]["score"] == 0.8
    assert report["cases"][0]["ragas"]["scores"]["faithfulness"] == 0.8
    assert report["summary"]["headline"]["faithfulness"] == 0.8
    assert report["summary"]["metric_sources"]["faithfulness"] == "ragas"


def test_evaluate_cli_keep_local_judge_preserves_local_fields(tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "report.json"
    _write_dataset(dataset)
    judge = Judge()

    def factory(settings, answer_provider, judge_provider):
        return Service(), judge

    result = evaluate_main(
        ["--dataset", str(dataset), "--output", str(output), "--ragas",
         "--ragas-metrics", "faithfulness", "--keep-local-judge"],
        factory=factory,
        ragas_factory=lambda report, config: _block(),
    )
    assert result == 0
    assert judge.calls
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["cases"][0]["faithfulness"]["reason"] == "local"
    assert report["cases"][0]["ragas"]["scores"]["faithfulness"] == 0.8
    assert report["summary"]["headline"]["faithfulness"] == 0.8


def test_evaluate_cli_ragas_failure_keeps_local_report(tmp_path, capsys):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "report.json"
    _write_dataset(dataset)

    def broken_ragas(report, config):
        raise RuntimeError("secret prompt")

    result = evaluate_main(
        ["--dataset", str(dataset), "--output", str(output), "--ragas",
         "--ragas-metrics", "faithfulness"],
        factory=lambda *_: (Service(), Judge()),
        ragas_factory=broken_ragas,
    )
    assert result == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ragas"]["error"] == "RAGAS_FAILED"
    assert "secret" not in capsys.readouterr().err


def test_evaluate_cli_rejects_ragas_flags_without_ragas(tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "report.json"
    _write_dataset(dataset)
    assert evaluate_main(
        ["--dataset", str(dataset), "--output", str(output),
         "--ragas-metrics", "faithfulness"],
        factory=lambda *_: (Service(), Judge()),
    ) == 2
    assert not output.exists()


def test_ragas_evaluate_cli_writes_merged_and_raw_output(tmp_path):
    from medical_rag.ragas_evaluate import main as ragas_main

    dataset = tmp_path / "dataset.jsonl"
    report_path = tmp_path / "report.json"
    output = tmp_path / "ragas-report.json"
    raw = tmp_path / "ragas-raw.json"
    _write_dataset(dataset)
    report_path.write_text(json.dumps({
        "config": {"top_k": 5},
        "summary": {"precision": 0.2, "mrr": 0.5},
        "cases": [{"id": "1", "question": "q", "evidence": [], "answer": None}],
    }, ensure_ascii=False), encoding="utf-8")
    calls = []

    def runner(report, config):
        calls.append(config)
        return _block()

    result = ragas_main(
        ["--dataset", str(dataset), "--report", str(report_path),
         "--output", str(output), "--raw-output", str(raw), "--allow-paid-model",
         "--metrics", "faithfulness"],
        runner=runner,
    )
    assert result == 0
    assert calls and calls[0].metrics == ("faithfulness",)
    merged = json.loads(output.read_text(encoding="utf-8"))
    assert merged["cases"][0]["ragas"]["scores"]["faithfulness"] == 0.8
    assert merged["cases"][0].get("faithfulness") is None
    assert json.loads(raw.read_text(encoding="utf-8"))["summary"]["faithfulness"] == 0.8


def test_ragas_evaluate_cli_requires_paid_flag_and_safe_paths(tmp_path, capsys):
    from medical_rag.ragas_evaluate import main as ragas_main

    dataset = tmp_path / "dataset.jsonl"
    report_path = tmp_path / "report.json"
    _write_dataset(dataset)
    report_path.write_text('{"cases":[{"id":"1","question":"q","evidence":[]}]}', encoding="utf-8")
    called = []

    def runner(report, config):
        called.append(1)
        return _block()

    assert ragas_main(["--dataset", str(dataset), "--report", str(report_path),
                       "--output", str(tmp_path / "out.json")], runner=runner) == 2
    assert not called
    assert "allow-paid-model" in capsys.readouterr().err
    assert ragas_main(["--dataset", str(dataset), "--report", str(report_path),
                       "--output", str(report_path), "--allow-paid-model"], runner=runner) == 2
    assert not called


def test_ragas_evaluate_cli_rejects_unknown_report_id(tmp_path):
    from medical_rag.ragas_evaluate import main as ragas_main

    dataset = tmp_path / "dataset.jsonl"
    report_path = tmp_path / "report.json"
    _write_dataset(dataset)
    report_path.write_text('{"cases":[{"id":"other","question":"q"}]}', encoding="utf-8")
    assert ragas_main(["--dataset", str(dataset), "--report", str(report_path),
                       "--output", str(tmp_path / "out.json"), "--allow-paid-model"],
                      runner=lambda report, config: _block("other")) == 2


def test_ragas_evaluate_cli_failure_writes_error_report(tmp_path, capsys):
    from medical_rag.ragas_evaluate import main as ragas_main

    dataset = tmp_path / "dataset.jsonl"
    report_path = tmp_path / "report.json"
    output = tmp_path / "out.json"
    _write_dataset(dataset)
    report_path.write_text('{"cases":[{"id":"1","question":"q","evidence":[]}]}', encoding="utf-8")

    def broken(report, config):
        raise RuntimeError("secret key")

    assert ragas_main(["--dataset", str(dataset), "--report", str(report_path),
                       "--output", str(output), "--allow-paid-model"],
                      runner=broken) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["ragas"]["error"] == "RAGAS_FAILED"
    assert "secret" not in capsys.readouterr().err
