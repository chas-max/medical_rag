"""Shared case scoring and summaries for legacy and workflow evaluation."""

from .evaluation_metrics import (
    cited_retrieval_scores,
    mean_or_none,
    parent_window_scores,
    retrieval_scores,
)
from .workflow.nodes.question import _split_facets


def new_case(case: dict) -> dict:
    """Initialize the compatible report schema and abstention expectations."""
    result = {"id": case["id"], "question": case["question"],
              "relevant_chunk_ids": case["relevant_chunk_ids"],
              "retrieval": None, "retrieval_parent_window": None,
              "post_rag_cited": None, "evidence": [], "answer": None, "abstained": None,
              "faithfulness": None, "context_relevance": None,
              "context_relevance_cited": None, "subquery_context_relevance": None,
              "answer_relevance": None,
              "error": None, "judge_errors": []}
    for field in ("reference_answer", "key_points", "answerability", "category",
                  "question_type", "difficulty", "source_refs", "chunk_grades",
                  "context_chunk_ids"):
        if field in case:
            result[field] = case[field]
    insufficient = case.get("answerability") == "insufficient_evidence"
    if insufficient:
        result.update({"expected_abstention": True, "correct_abstention": None,
                       "unsupported_answer": None, "context_hit_count": 0})
    return result


def score_evidence(result: dict, evidence: list, top_k: int) -> None:
    """Score the ordered snapshot while retaining every judging context."""
    if not result.get("expected_abstention"):
        relevant_ids = set(result["relevant_chunk_ids"])
        result["retrieval"] = retrieval_scores(
            [item.chunk.id for item in evidence],
            relevant_ids,
            top_k,
            chunk_grades=result.get("chunk_grades"),
        )
        result["retrieval_parent_window"] = parent_window_scores(
            evidence,
            relevant_ids,
            top_k,
            chunk_grades=result.get("chunk_grades"),
            source_refs=result.get("source_refs"),
        )
    else:
        context_ids = set(result.get("context_chunk_ids") or ())
        result["context_hit_count"] = sum(1 for item in evidence if item.chunk.id in context_ids)
    result["evidence"] = [{"chunk_id": item.chunk.id, "text": item.chunk.context_text}
                          for item in evidence]


def score_answer(result: dict, answer: dict, evidence: list, judge: object) -> None:
    """Keep abstention semantics and dimension-local sanitized judge failures."""
    result["answer"] = answer
    result["abstained"] = answer["abstained"]
    question = result["question"]
    if result.get("expected_abstention"):
        result["correct_abstention"] = bool(result["abstained"])
        result["unsupported_answer"] = not result["abstained"]
        return
    if result["abstained"]:
        return

    cited_ids = [
        ref["chunk_id"]
        for ref in (answer.get("citations") or ())
        if isinstance(ref, dict) and isinstance(ref.get("chunk_id"), str)
    ]
    if result.get("relevant_chunk_ids") or result.get("source_refs"):
        result["post_rag_cited"] = cited_retrieval_scores(
            cited_ids,
            evidence,
            set(result["relevant_chunk_ids"]),
            chunk_grades=result.get("chunk_grades"),
            source_refs=result.get("source_refs"),
        )

    def try_judge(dimension, method, *args):
        try:
            return method(*args)
        except Exception:  # noqa: BLE001 -- judge failures affect only this dimension
            result["judge_errors"].append(dimension + "_FAILED")
            return None

    texts = [item.chunk.context_text for item in evidence]
    result["faithfulness"] = try_judge("FAITHFULNESS", judge.score_faithfulness,
                                        question, answer["answer"], texts)
    details = []
    for item in evidence:
        score = try_judge("CONTEXT_RELEVANCE", judge.score_context,
                          question, item.chunk.context_text)
        details.append({"chunk_id": item.chunk.id, "result": score})
    context_scores = [item["result"]["score"] for item in details if item["result"] is not None]
    result["context_relevance"] = {
        "score": mean_or_none(context_scores) if len(context_scores) == len(details) else None,
        "items": details, "model": judge.model,
    } if details else None

    cited_set = set(cited_ids)
    cited_details = [item for item in details if item["chunk_id"] in cited_set]
    cited_scores = [item["result"]["score"] for item in cited_details if item["result"] is not None]
    result["context_relevance_cited"] = {
        "score": mean_or_none(cited_scores) if len(cited_scores) == len(cited_details) else None,
        "items": cited_details, "model": judge.model,
    } if cited_details else None

    subqueries = _split_facets(question, max_subqueries=3)
    if len(subqueries) >= 2 and details and hasattr(judge, "score_context_subquery"):
        sub_details = []
        for item in evidence:
            sub_score = try_judge(
                "SUBQUERY_CONTEXT_RELEVANCE",
                judge.score_context_subquery,
                question,
                subqueries,
                item.chunk.context_text,
            )
            sub_details.append({"chunk_id": item.chunk.id, "result": sub_score})
        sub_scores = [entry["result"]["score"] for entry in sub_details if entry["result"] is not None]
        result["subquery_context_relevance"] = {
            "score": mean_or_none(sub_scores) if len(sub_scores) == len(sub_details) else None,
            "items": sub_details, "model": judge.model,
        }
    else:
        result["subquery_context_relevance"] = result["context_relevance"]

    result["answer_relevance"] = try_judge("ANSWER_RELEVANCE", judge.score_answer,
                                            question, answer["answer"])


def build_report(results: list[dict], judge: object, top_k: int,
                 answer_provider: str, judge_provider: str) -> dict:
    """Aggregate only valid scores; failed judgments never become zeros."""
    valid_retrieval = [item["retrieval"] for item in results
                       if item["retrieval"] is not None and item.get("answerability") != "insufficient_evidence"]
    valid_parent = [item["retrieval_parent_window"] for item in results
                    if item.get("retrieval_parent_window") is not None
                    and item.get("answerability") != "insufficient_evidence"]
    valid_cited = [item["post_rag_cited"] for item in results
                   if item.get("post_rag_cited") is not None
                   and item.get("answerability") != "insufficient_evidence"]
    dimensions = (
        "faithfulness",
        "context_relevance",
        "context_relevance_cited",
        "subquery_context_relevance",
        "answer_relevance",
    )
    valid_scores = {name: [item[name]["score"] for item in results
                           if item.get(name) is not None and item[name]["score"] is not None]
                    for name in dimensions}
    abstentions = sum(item["abstained"] is True for item in results)
    summary = {name: mean_or_none([item[name] for item in valid_retrieval])
               for name in ("precision", "recall", "f1", "ndcg", "core_recall", "hit_rate")}
    summary.update({"map": mean_or_none([item["ap"] for item in valid_retrieval]),
                    "mrr": mean_or_none([item["rr"] for item in valid_retrieval]),
                    "retrieval_valid_count": len(valid_retrieval),
                    "parent_precision": mean_or_none([item["precision"] for item in valid_parent]),
                    "parent_r_precision": mean_or_none([item["r_precision"] for item in valid_parent]),
                    "parent_recall": mean_or_none([item["recall"] for item in valid_parent]),
                    "parent_core_recall": mean_or_none([item["core_recall"] for item in valid_parent]),
                    "parent_f1": mean_or_none([item["f1"] for item in valid_parent]),
                    "parent_ndcg": mean_or_none([item["ndcg"] for item in valid_parent]),
                    "parent_mrr": mean_or_none([item["rr"] for item in valid_parent]),
                    "parent_hit_rate": mean_or_none([item["hit_rate"] for item in valid_parent]),
                    "post_rag_cited_precision": mean_or_none([item["precision"] for item in valid_cited]),
                    "post_rag_cited_recall": mean_or_none([item["recall"] for item in valid_cited]),
                    "post_rag_cited_core_recall": mean_or_none([item["core_recall"] for item in valid_cited]),
                    "post_rag_cited_f1": mean_or_none([item["f1"] for item in valid_cited]),
                    "post_rag_cited_child_precision": mean_or_none([item["child_precision"] for item in valid_cited]),
                    "post_rag_cited_child_recall": mean_or_none([item["child_recall"] for item in valid_cited]),
                    "post_rag_cited_child_f1": mean_or_none([item["child_f1"] for item in valid_cited]),
                    "abstention_count": abstentions,
                    "abstention_rate": abstentions / len(results),
                    "judge_failures": sum(len(item["judge_errors"]) for item in results),
                    "valid_counts": {name: len(values) for name, values in valid_scores.items()}})
    summary.update({name: mean_or_none(values) for name, values in valid_scores.items()})
    abstention_cases = [item for item in results if item.get("expected_abstention")]
    correct_abstentions = [item["correct_abstention"] for item in abstention_cases
                           if item["correct_abstention"] is not None]
    unsupported = [item["unsupported_answer"] for item in abstention_cases
                   if item["unsupported_answer"] is not None]
    decoy_abstentions = [float(item["correct_abstention"]) for item in abstention_cases
                         if (item.get("context_hit_count") or 0) > 0
                         and item["correct_abstention"] is not None]
    summary.update({"expected_abstention_count": len(abstention_cases),
                    "correct_abstention_count": sum(correct_abstentions),
                    "correct_abstention_rate": mean_or_none(correct_abstentions),
                    "unsupported_answer_count": sum(unsupported),
                    "unsupported_answer_rate": mean_or_none(unsupported),
                    "decoy_abstention_count": sum(1 for item in decoy_abstentions if item),
                    "decoy_abstention_rate": mean_or_none(decoy_abstentions)})
    # Diagnostic breakdown separating resolver clarification halts from retrieved turns.
    clarifications = sum(
        isinstance(item.get("answer"), dict) and item["answer"].get("answer_type") == "clarification"
        for item in results
    )
    workflow_failed = sum(item.get("error") == "WORKFLOW_FAILED" for item in results)
    executed_retrieval = [
        item["retrieval"] for item in results
        if item["retrieval"] is not None
        and item.get("answerability") != "insufficient_evidence"
        and not (isinstance(item.get("answer"), dict) and item["answer"].get("answer_type") == "clarification")
    ]
    executed_parent = [
        item["retrieval_parent_window"] for item in results
        if item.get("retrieval_parent_window") is not None
        and item.get("answerability") != "insufficient_evidence"
        and not (isinstance(item.get("answer"), dict) and item["answer"].get("answer_type") == "clarification")
    ]
    summary.update({
        "clarification_halt_count": clarifications,
        "clarification_halt_rate": clarifications / len(results),
        "workflow_failed_count": workflow_failed,
        "executed_retrieval_valid_count": len(executed_retrieval),
        "executed_precision": mean_or_none([item["precision"] for item in executed_retrieval]),
        "executed_recall": mean_or_none([item["recall"] for item in executed_retrieval]),
        "executed_core_recall": mean_or_none([item["core_recall"] for item in executed_retrieval]),
        "executed_ndcg": mean_or_none([item["ndcg"] for item in executed_retrieval]),
        "executed_hit_rate": mean_or_none([item["hit_rate"] for item in executed_retrieval]),
        "executed_parent_r_precision": mean_or_none([item["r_precision"] for item in executed_parent]),
        "executed_parent_recall": mean_or_none([item["recall"] for item in executed_parent]),
        "executed_parent_core_recall": mean_or_none([item["core_recall"] for item in executed_parent]),
        "executed_parent_mrr": mean_or_none([item["rr"] for item in executed_parent]),
    })
    return {"config": {"top_k": top_k, "answer_provider": answer_provider,
                       "judge_provider": judge_provider, "judge_model": judge.model},
            "cases": results, "summary": summary}
