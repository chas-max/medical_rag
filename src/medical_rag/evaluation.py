"""Offline dataset validation and end-to-end RAG evaluation reports."""

import json
from pathlib import Path

from .evaluation_metrics import mean_or_none, retrieval_scores


def load_dataset(path: Path) -> list[dict]:
    """Read and validate every JSONL row before permitting any model calls."""
    cases = []
    ids = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {line_number} 行不是有效 JSON") from exc
        if not isinstance(row, dict) or not {"question", "relevant_chunk_ids"} <= row.keys() or not row.keys() <= {"id", "question", "relevant_chunk_ids"}:
            raise ValueError(f"第 {line_number} 行字段不符合要求")
        question, relevant = row["question"], row["relevant_chunk_ids"]
        identifier = row.get("id", str(line_number))
        if (not isinstance(question, str) or not question.strip()
                or not isinstance(identifier, str) or not identifier.strip() or identifier in ids
                or not isinstance(relevant, list) or not relevant
                or any(not isinstance(item, str) or not item.strip() for item in relevant)
                or len(relevant) != len(set(relevant))):
            raise ValueError(f"第 {line_number} 行题目或相关块 ID 无效")
        ids.add(identifier)
        cases.append({"id": identifier, "question": question.strip(), "relevant_chunk_ids": relevant})
    if not cases:
        raise ValueError("题集为空")
    return cases


def evaluate_cases(cases: list[dict], service: object, judge: object, top_k: int,
                   answer_provider: str, judge_provider: str) -> dict:
    """Reuse each search result for both scoring and generation."""
    if not 1 <= top_k <= 20 or not cases:
        raise ValueError("top_k 必须在 1 到 20 之间，题集不能为空")
    results = []
    judge_failures = 0
    for case in cases:
        question = case["question"]
        result = {"id": case["id"], "question": question,
                  "relevant_chunk_ids": case["relevant_chunk_ids"],
                  "retrieval": None, "evidence": [], "answer": None, "abstained": None,
                  "faithfulness": None, "context_relevance": None, "answer_relevance": None,
                  "error": None, "judge_errors": []}
        results.append(result)
        try:
            evidence = service.search(question, top_k)
            result["retrieval"] = retrieval_scores(
                [item.chunk.id for item in evidence], set(case["relevant_chunk_ids"]), top_k)
            result["evidence"] = [{"chunk_id": item.chunk.id, "text": item.chunk.text}
                                  for item in evidence]
        except Exception:
            result["error"] = "SEARCH_FAILED"
            continue
        try:
            answer = service.generator.answer(question, evidence, answer_provider)
            result["answer"] = answer
            result["abstained"] = answer["abstained"]
        except Exception:
            result["error"] = "ANSWER_FAILED"
            continue
        if result["abstained"]:
            continue

        def try_judge(dimension, method, *args):
            nonlocal judge_failures
            try:
                return method(*args)
            except Exception:
                judge_failures += 1
                result["judge_errors"].append(dimension + "_FAILED")
                return None

        texts = [item.chunk.text for item in evidence]
        result["faithfulness"] = try_judge("FAITHFULNESS", judge.score_faithfulness,
                                            question, answer["answer"], texts)
        details = []
        for item in evidence:
            score = try_judge("CONTEXT_RELEVANCE", judge.score_context, question, item.chunk.text)
            details.append({"chunk_id": item.chunk.id, "result": score})
        context_scores = [item["result"]["score"] for item in details if item["result"] is not None]
        result["context_relevance"] = {
            "score": mean_or_none(context_scores) if len(context_scores) == len(details) else None,
            "items": details, "model": judge.model,
        } if details else None
        result["answer_relevance"] = try_judge("ANSWER_RELEVANCE", judge.score_answer,
                                                question, answer["answer"])

    valid_retrieval = [item["retrieval"] for item in results if item["retrieval"] is not None]
    dimensions = ("faithfulness", "context_relevance", "answer_relevance")
    valid_scores = {name: [item[name]["score"] for item in results
                           if item[name] is not None and item[name]["score"] is not None]
                    for name in dimensions}
    abstentions = sum(item["abstained"] is True for item in results)
    summary = {name: mean_or_none([item[name] for item in valid_retrieval])
               for name in ("precision", "recall", "f1")}
    summary.update({"map": mean_or_none([item["ap"] for item in valid_retrieval]),
                    "mrr": mean_or_none([item["rr"] for item in valid_retrieval]),
                    "retrieval_valid_count": len(valid_retrieval),
                    "abstention_count": abstentions,
                    "abstention_rate": abstentions / len(results),
                    "judge_failures": judge_failures,
                    "valid_counts": {name: len(values) for name, values in valid_scores.items()}})
    summary.update({name: mean_or_none(values) for name, values in valid_scores.items()})
    return {"config": {"top_k": top_k, "answer_provider": answer_provider,
                       "judge_provider": judge_provider, "judge_model": judge.model},
            "cases": results, "summary": summary}
