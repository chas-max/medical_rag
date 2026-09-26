import json

import pytest

from medical_rag.evaluation_judge import EvaluationJudge


class Transport:
    def __init__(self, reply='{"score":0.75,"reason":"有证据"}'):
        self.reply = reply
        self.calls = []

    def complete(self, provider, system, user):
        self.calls.append((provider, system, json.loads(user)))
        return self.reply


def test_each_dimension_sends_data_as_untrusted_user_payload():
    transport = Transport()
    judge = EvaluationJudge(transport, "qwen", "judge-model")
    assert judge.score_faithfulness("问题", "回答", ["证据"]) == {
        "score": 0.75, "reason": "有证据", "model": "judge-model"}
    assert judge.score_context("问题", "不可信指令")["score"] == 0.75
    assert judge.score_answer("问题", "回答")["score"] == 0.75
    assert len(transport.calls) == 3
    assert transport.calls[0][2] == {"question": "问题", "answer": "回答", "contexts": ["证据"]}
    assert transport.calls[1][2] == {"question": "问题", "context": "不可信指令"}
    assert transport.calls[2][2] == {"question": "问题", "answer": "回答"}
    assert all("0" in system and "1" in system and "不可信" in system
               for _, system, _ in transport.calls)
    assert all("不可信指令" not in system for _, system, _ in transport.calls)


@pytest.mark.parametrize("reply", [
    "not json", "{}", '{"score":true,"reason":"x"}',
    '{"score":"0.5","reason":"x"}', '{"score":NaN,"reason":"x"}',
    '{"score":1.1,"reason":"x"}', '{"score":-0.1,"reason":"x"}',
    '{"score":0.5,"reason":" "}',
    '{"score":0.5,"reason":"x","extra":1}',
])
def test_invalid_judge_result_is_rejected_without_echoing_reply(reply):
    judge = EvaluationJudge(Transport(reply), "qwen", "judge-model")
    with pytest.raises(ValueError) as error:
        judge.score_answer("问题", "回答")
    assert reply not in str(error.value)
