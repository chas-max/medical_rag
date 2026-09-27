"""Offline dataset validation and end-to-end RAG evaluation reports."""

from pathlib import Path

from .evaluation_metrics import mean_or_none, retrieval_scores
from .evaluation_dataset import load_evaluation_dataset


def load_dataset(path: Path) -> list[dict]:
    """Read and validate every JSONL row before permitting any model calls."""
    cases = load_evaluation_dataset(path)
    for case in cases:
        if "answerability" not in case and not case["relevant_chunk_ids"]:
            raise ValueError("第 1 行题目或相关块 ID 无效")
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
        for field in ("reference_answer", "key_points", "answerability", "category",
                      "question_type", "difficulty", "source_refs"):
            if field in case:
                result[field] = case[field]
        insufficient = case.get("answerability") == "insufficient_evidence"
        if insufficient:
            result.update({"expected_abstention": True, "correct_abstention": None,
                           "unsupported_answer": None})
        results.append(result)
        try:
            evidence = service.search(question, top_k)
            if not insufficient:
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
        if insufficient:
            result["correct_abstention"] = bool(result["abstained"])
            result["unsupported_answer"] = not result["abstained"]
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

    valid_retrieval = [item["retrieval"] for item in results
                       if item["retrieval"] is not None and item.get("answerability") != "insufficient_evidence"]
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
    abstention_cases = [item for item in results if item.get("expected_abstention")]
    correct_abstentions = [item["correct_abstention"] for item in abstention_cases
                           if item["correct_abstention"] is not None]
    unsupported = [item["unsupported_answer"] for item in abstention_cases
                   if item["unsupported_answer"] is not None]
    summary.update({"expected_abstention_count": len(abstention_cases),
                    "correct_abstention_count": sum(correct_abstentions),
                    "correct_abstention_rate": mean_or_none(correct_abstentions),
                    "unsupported_answer_count": sum(unsupported),
                    "unsupported_answer_rate": mean_or_none(unsupported)})
    return {"config": {"top_k": top_k, "answer_provider": answer_provider,
                       "judge_provider": judge_provider, "judge_model": judge.model},
            "cases": results, "summary": summary}
