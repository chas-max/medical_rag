import json

from medical_rag.evaluate import main


class Judge:
    model = "judge"

    def score_faithfulness(self, question, answer, contexts):
        return {"score": 1, "reason": "支持", "model": self.model}

    def score_context(self, question, context):
        return {"score": 1, "reason": "相关", "model": self.model}

    def score_answer(self, question, answer):
        return {"score": 1, "reason": "回答", "model": self.model}


class Generator:
    def answer(self, question, evidence, provider):
        return {"answer": "资料不足", "citations": [], "abstained": True}


class Service:
    generator = Generator()

    def search(self, question, top_k):
        return []


def test_cli_validates_before_factory(tmp_path):
    source = tmp_path / "bad.jsonl"
    source.write_text("{}", encoding="utf-8")
    calls = []

    def factory(*args):
        calls.append(args)
        return Service(), Judge()

    assert main(["--dataset", str(source), "--output", str(tmp_path / "out.json")], factory=factory) != 0
    assert not calls
    assert main(["--dataset", str(source), "--output", str(source)], factory=factory) != 0
    assert not calls


def test_cli_writes_report_without_network(tmp_path):
    source = tmp_path / "dataset.jsonl"
    output = tmp_path / "report.json"
    source.write_text('{"question":"问题","relevant_chunk_ids":["id"]}', encoding="utf-8")
    assert main(["--dataset", str(source), "--output", str(output), "--top-k", "2"],
                factory=lambda *_: (Service(), Judge())) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["summary"]["recall"] == 0
    assert result["config"]["top_k"] == 2
    assert result["summary"]["abstention_count"] == 1


def test_cli_fails_safely_on_init_and_write(tmp_path, capsys):
    source = tmp_path / "dataset.jsonl"
    source.write_text('{"question":"问题","relevant_chunk_ids":["id"]}', encoding="utf-8")

    def broken(*args):
        raise RuntimeError("secret credential")

    assert main(["--dataset", str(source), "--output", str(tmp_path / "out.json")], factory=broken) != 0
    assert "secret" not in capsys.readouterr().err
    assert main(["--dataset", str(source), "--output", str(tmp_path / "missing" / "out.json")],
                factory=lambda *_: (Service(), Judge())) != 0
    assert main(["--dataset", str(source), "--output", str(tmp_path / "out.json"), "--top-k", "0"],
                factory=lambda *_: (Service(), Judge())) != 0


def test_cli_versioned_release_checks_runtime_mapping_before_model_calls(tmp_path):
    source = tmp_path / "runtime.jsonl"
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "report.json"
    row = {"id": "MHE-0001", "question": "如何测量？", "reference_answer": "按规范测量。",
           "key_points": ["规范测量", "记录"], "answerability": "answerable",
           "category": "管理", "question_type": "procedure", "difficulty": "easy",
           "source_refs": [{"source_id": "doc", "locator": {"page": 1},
                            "quote": "按规范测量。", "anchor_id": "doc#1"}],
           "relevant_chunk_ids": []}
    source.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest.write_text(json.dumps({"question_count": 1, "dataset_version": "v1.0",
                                    "rubric_version": "r1.0", "quotas": {
                                        "answerability": {"answerable": 1}}}), encoding="utf-8")
    calls = []

    def factory(*args):
        calls.append(args)
        return Service(), Judge()

    assert main(["--dataset", str(source), "--manifest", str(manifest),
                 "--output", str(output)], factory=factory) == 2
    assert calls == []
    row["relevant_chunk_ids"] = ["c1"]
    source.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    assert main(["--dataset", str(source), "--manifest", str(manifest),
                 "--output", str(output)], factory=factory) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["dataset_version"] == "v1.0"
    assert report["rubric_version"] == "r1.0"
    assert report["summary"]["answerability_counts"] == {"answerable": 1}
