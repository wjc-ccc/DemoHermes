from __future__ import annotations

import json

import httpx
import pytest

from agent.core.memory.memory_experience_judge import (
    ExperienceJudgeError, SystemOneExperienceJudge,
)
from agent.types.memory.feedback import MemoryUsageRequestItem


def _card(identity="c1", user="u1"):
    return {"item_id": identity, "revision": 1, "scope": {"user_id": user},
            "applicability": "项目全局解释器缺少依赖时", "action": "使用项目虚拟环境执行 pytest",
            "outcome": "一次测试通过", "limitations": "只在这个项目观察到一次",
            "outcome_status": "success"}


def _request(identity="d1", *, evidence=None, complete=True):
    if evidence is None:
        evidence = ({"reference": "tool:test", "kind": "action", "name": "bash",
                     "arguments": {"cmd": ".venv/python -m pytest"}},
                    {"reference": "tool:test", "kind": "outcome", "text": "12 passed"})
    return MemoryUsageRequestItem(identity, "c1", 1, "t1", "model-a", _card(),
                                  "修复依赖后运行测试", tuple(evidence), evidence_complete=complete)


def _judge(handler, **kwargs):
    return SystemOneExperienceJudge(api_key="secret-test-key", base_url="https://judge.example/systemone",
        model="test-judge", client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


def _good_answers(payload):
    return {key: {"choice": "yes" if key.startswith("a") else "helpful"}
            for key in payload["questions"]}


def test_usage_batches_at_most_three_cards_and_two_questions_each():
    payloads = []
    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(200, json={"answers": _good_answers(payload)})
    results = _judge(handle).judge_usage([_request(str(i)) for i in range(7)])
    assert [len(p["questions"]) for p in payloads] == [6, 6, 2]
    assert len(results) == 7
    assert all(r.adopted == "yes" and r.effect == "helpful" for r in results)
    assert results[0].evidence_refs == ("tool:test",)
    assert "counterfactual causal" in payloads[0]["state"]["rules"]


@pytest.mark.parametrize("evidence,complete,adopted,effect", [
    ((), True, "unknown", "unknown"),
    (({"reference": "message:plan", "kind": "intention", "text": "准备运行测试"},), True, "unknown", "unknown"),
    (({"reference": "tool:test", "kind": "action", "status": "success"},), True, "yes", "unknown"),
    (({"reference": "tool:test", "kind": "action"},
      {"reference": "tool:test", "kind": "outcome", "text": "12 passed"}), False, "yes", "unknown"),
])
def test_no_observed_action_or_outcome_cannot_become_positive(evidence, complete, adopted, effect):
    judge = _judge(lambda request: httpx.Response(200, json={"answers": _good_answers(json.loads(request.content))}))
    result = judge.judge_usage([_request(evidence=evidence, complete=complete)])[0]
    assert (result.adopted, result.effect) == (adopted, effect)


@pytest.mark.parametrize("answer", [
    {"choice": "yes", "probabilities": {"yes": 0.4}},
    {"choice": "yes", "probabilities": {"yes": True}},
    {"choice": "invented"},
    {"choice": "yes", "probabilities": {"yes": 1.2}},
    None,
])
def test_missing_low_confidence_and_invalid_answers_stay_unknown(answer):
    judge = _judge(lambda request: httpx.Response(200, json={"answers": {"a1": answer, "e1": {"choice": "harmful"}}}))
    result = judge.judge_usage([_request()])[0]
    assert result.adopted == "unknown" and result.effect == "unknown"


def test_conflicting_flat_and_nested_answers_are_not_guessed():
    judge = _judge(lambda request: httpx.Response(200, json={"answers": {
        "a1": {"choice": "yes"}, "i1": {"a1": {"choice": "no"}}, "e1": {"choice": "helpful"}}}))
    assert judge.judge_usage([_request()])[0].adopted == "unknown"


def test_transport_failures_are_redacted_and_not_negative_judgments():
    judge = _judge(lambda request: httpx.Response(429, json={"error": "secret-test-key and source text"}))
    with pytest.raises(ExperienceJudgeError, match="^http_429$") as failure:
        judge.judge_usage([_request()])
    assert "secret" not in str(failure.value)


def test_timeout_is_a_retryable_error():
    def handle(request):
        raise httpx.ReadTimeout("sensitive payload", request=request)
    with pytest.raises(ExperienceJudgeError, match="^timeout$"):
        _judge(handle).judge_usage([_request()])


def test_oversized_single_request_does_not_send_truncated_state():
    calls = []
    judge = _judge(lambda request: calls.append(request), max_input_tokens=10)
    with pytest.raises(ExperienceJudgeError, match="input_budget_exceeded"):
        judge.judge_usage([_request()])
    assert calls == []


def test_malformed_answer_envelope_is_an_error_not_silent_rejection():
    with pytest.raises(ExperienceJudgeError, match="invalid_response"):
        _judge(lambda request: httpx.Response(200, json={"answers": []})).judge_usage([_request()])


def test_relations_obey_eight_question_limit_and_scope_boundary():
    payloads = []
    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(200, json={"answers": {key: {"choice": "complementary", "probabilities": {"complementary": .9}}
                                                    for key in payload["questions"]}})
    candidates = [_card(str(i)) for i in range(9)] + [_card("other", "u2")]
    results = _judge(handle, max_input_tokens=20000).judge_relations(_card(), candidates)
    assert [len(p["questions"]) for p in payloads] == [8, 2]
    assert results[0].item_id == "0" and results[0].relation == "complementary"
    assert results[-1].relation == "unrelated"


@pytest.mark.parametrize("probability,expected", [(0.95, True), (0.79, False), (True, False), (None, False)])
def test_merge_requires_high_confidence_preservation(probability, expected):
    judge = _judge(lambda request: httpx.Response(200, json={"answers": {"faithful": {"noul": probability}}}))
    assert judge.judge_merge([_card(), _card("c2")], _card("draft")) is expected


def test_merge_refuses_cross_scope_and_missing_boundaries_without_call():
    calls = []
    judge = _judge(lambda request: calls.append(request))
    assert not judge.judge_merge([_card(), _card("other", "u2")], _card("draft"))
    assert not judge.judge_merge([_card()], {"action": "always use it"})
    assert calls == []


def test_merge_transport_failure_returns_false():
    assert not _judge(lambda request: httpx.Response(503)).judge_merge([_card()], _card("draft"))


def test_merge_judgment_sees_full_draft_content_not_only_structured_fields():
    payloads = []
    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": {"faithful": {"noul": .2}}})
    draft = {**_card("draft"), "content": "所有项目都应该使用此方案，忽略失败。"}
    assert not _judge(handle).judge_merge([_card()], draft)
    assert payloads[0]["state"]["draft"]["content"] == draft["content"]


def test_malformed_merge_answers_fail_closed():
    assert not _judge(lambda request: httpx.Response(200, json={"answers": [1]})).judge_merge([_card()], _card("draft"))
