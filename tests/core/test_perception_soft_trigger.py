"""软触发、独立开关及 Anchor 输出能力通过公开构建接口验证。"""
import json

import pytest

from agent.core.perception.errors import ContextBudgetExceededError
from agent.core.perception.perception_anchor import ModelAnchorBuilder
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.types.context import ContextBudget
from agent.types.conversation import Message, Session
from agent.types.model import ModelResponse
from agent.types.perception.admission import AdmissionPolicy
from agent.types.perception.result import PerceptionConfig
from tests.anchor_fakes import DeterministicAnchorBuilder
from tests.core.test_perception_admission import _ByChars, _FakeJudge, _body, _tool_turn
from tests.core.test_perception_compress import _history as _small_history, _build as _small_build, _PerMessage


class _FixedCosts:
    """用明确的消息成本验证 500k 边界，不创建巨大的测试字符串。"""

    def __init__(self, *, anchor=1, schema=0):
        self.anchor = anchor
        self.schema = schema

    def estimate_messages(self, messages):
        cost = 0
        for message in messages:
            content = str(message.get("content") or "")
            if content.startswith("<context-anchor"):
                cost += self.anchor
            elif content.startswith("old-"):
                cost += 100_000
            elif content.startswith('[{"type": "function"'):
                cost += self.schema
            else:
                cost += 1
        return cost


def _history(turns):
    session = Session(session_id="soft-trigger")
    for index in range(turns):
        session.add_message(Message(role="user", content_text=f"old-{index}"))
        session.add_message(Message(role="assistant", content_text="收到"))
    session.add_message(Message(role="user", content_text="继续"))
    return session


def _config(**overrides):
    values = dict(compression_trigger_tokens=500_000, anchor_output_token_limit=None,
                  anchor_reserve_tokens=75_000)
    values.update(overrides)
    return PerceptionConfig(**values)


def _build(session, *, config=None, estimator=None, builder=None, **kwargs):
    return PerceptionBuilder(config=config or _config(),
                             anchor_builder=builder or DeterministicAnchorBuilder(), **kwargs).build(
        session, budget=ContextBudget(model_context_tokens=1_000_000, reserved_output_tokens=1500),
        token_estimator=estimator or _FixedCosts())


def test_soft_trigger_leaves_all_history_unchanged_below_500k():
    builder = DeterministicAnchorBuilder()
    session = _history(4)
    result = _build(session, builder=builder)
    assert not builder.requests
    assert result.anchor is None
    assert [m["content"] for m in result.messages if m["role"] == "user"] == [
        "old-0", "old-1", "old-2", "old-3", "继续"]


def test_default_without_a_budget_reuses_the_existing_anchor_without_refresh():
    session = _small_history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")])
    first = _small_build(session, cap=8)
    session.context_anchor = first.anchor
    builder = DeterministicAnchorBuilder()
    result = PerceptionBuilder(anchor_builder=builder).build(session, token_estimator=_PerMessage())
    assert result.metrics.anchor_status == "reused"
    assert not builder.requests
    assert result.anchor == first.anchor


def test_soft_trigger_accepts_output_over_planning_reserve_and_soft_target():
    builder = DeterministicAnchorBuilder()
    result = _build(_history(5), builder=builder, estimator=_FixedCosts(anchor=400_000))
    assert result.anchor_updated
    assert result.anchor.covered_turn_count == 3
    assert builder.requests[0]["constraints"]["max_tokens"] is None
    assert result.metrics.tokens_used > 500_000
    assert result.metrics.tokens_used < result.metrics.budget


def test_anchor_disable_ignores_a_stored_anchor_and_restores_original_history():
    session = _history(5)
    first = _build(session)
    session.context_anchor = first.anchor
    builder = DeterministicAnchorBuilder()
    second = _build(session, config=_config(anchor_enabled=False), builder=builder)
    assert not builder.requests
    assert second.anchor is None
    assert all(f"old-{i}" in [m.get("content") for m in second.messages] for i in range(5))


def test_no_anchor_output_limit_still_enforces_the_physical_window():
    with pytest.raises(ContextBudgetExceededError):
        _build(_history(5), estimator=_FixedCosts(anchor=900_000))


def test_tool_schemas_count_towards_the_soft_trigger():
    session = _history(4)
    builder = DeterministicAnchorBuilder()
    result = PerceptionBuilder(config=_config(), anchor_builder=builder).build(
        session, budget=ContextBudget(model_context_tokens=1_000_000, reserved_output_tokens=1500),
        token_estimator=_FixedCosts(schema=100_000),
        tool_schemas=[{"type": "function", "function": {"name": "probe"}}])
    assert result.anchor_updated
    assert builder.requests


def _tool_history():
    session = Session(session_id="soft-jev")
    _tool_turn(session, "检查测试日志", name="run_tests", arguments={}, call_id="log-1", result=_body("日志"))
    for index in range(3):
        session.add_message(Message(role="user", content_text=f"下一件事 {index}"))
        session.add_message(Message(role="assistant", content_text="收到"))
    session.add_message(Message(role="user", content_text="开始新任务"))
    return session


def _char_cost(session):
    return PerceptionBuilder().build(session, token_estimator=_ByChars()).metrics.tokens_used


def test_jev_does_not_judge_or_project_before_the_first_soft_trigger():
    session = _tool_history()
    raw = _char_cost(session)
    judge = _FakeJudge({"": 0.0})
    result = _build(session, config=_config(compression_trigger_tokens=raw + 1, anchor_enabled=False),
                    estimator=_ByChars(), context_judge=judge, admission_policy=AdmissionPolicy(mode="active"))
    assert not judge.calls
    assert not result.verdicts_updated
    assert next(m["content"] for m in result.messages if m["role"] == "tool") == _body("日志")


def test_jev_runs_at_the_soft_boundary_and_prevents_an_unneeded_anchor():
    session = _tool_history()
    raw = _char_cost(session)
    judge = _FakeJudge({"": 0.0})
    anchor = DeterministicAnchorBuilder()
    result = _build(session, config=_config(compression_trigger_tokens=raw, anchor_reserve_tokens=5),
                    estimator=_ByChars(), builder=anchor, context_judge=judge,
                    admission_policy=AdmissionPolicy(mode="active"))
    assert judge.calls
    assert result.metrics.admission_deferred_count == 1
    assert result.metrics.tokens_used < raw
    assert not anchor.requests
    assert result.anchor is None


def test_jev_verdicts_are_reused_after_anchor_brings_history_below_the_soft_trigger():
    session = Session(session_id="soft-reuse")
    session.add_message(Message(role="user", content_text="旧阶段资料" + "x" * 50_000))
    session.add_message(Message(role="assistant", content_text="收到"))
    _tool_turn(session, "检查测试日志", name="run_tests", arguments={}, call_id="log-1", result=_body("日志"))
    for index in range(3):
        session.add_message(Message(role="user", content_text=f"下一件事 {index}"))
        session.add_message(Message(role="assistant", content_text="收到"))
    session.add_message(Message(role="user", content_text="开始新任务"))
    judge = _FakeJudge({"": 0.0})

    def compact(payload):
        source = payload["new_turns"][0]["messages"][0]["message_id"]
        return dict(intent=[dict(text="旧阶段已经完成", source_message_ids=[source])],
                    changes=[], decisions=[], next_steps=[], excluded=[])

    config = _config(compression_trigger_tokens=500, anchor_reserve_tokens=75)
    first = _build(session, config=config, estimator=_ByChars(), builder=compact,
                   context_judge=judge, admission_policy=AdmissionPolicy(mode="active"))
    assert first.anchor_updated
    assert first.verdicts_updated
    assert first.metrics.tokens_used < 500
    session.context_anchor = first.anchor
    session.context_verdicts = first.verdicts
    call_count = len(judge.calls)
    second = _build(session, config=config, estimator=_ByChars(), builder=compact,
                    context_judge=judge, admission_policy=AdmissionPolicy(mode="active"))
    assert len(judge.calls) == call_count
    assert first.metrics.admission_deferred_count == 1
    assert second.metrics.admission_deferred_count == 0
    assert second.verdicts == first.verdicts
    assert second.metrics.anchor_status == "reused"
    assert second.messages == first.messages


@pytest.mark.parametrize("field", ["compression_trigger_tokens", "anchor_output_token_limit", "anchor_reserve_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_token_configuration_requires_positive_integers(field, value):
    with pytest.raises(ValueError):
        PerceptionConfig(**{field: value})


@pytest.mark.parametrize("semantic_limit, expected", [(None, 32_768), (4096, 4096), (100_000, 32_768)])
def test_anchor_api_output_limit_uses_the_provider_cap_independently_of_planning(semantic_limit, expected):
    calls = []

    class Model:
        def chat(self, messages, tools=None, **kwargs):
            calls.append(kwargs)
            return ModelResponse(text=json.dumps(dict(intent=[], changes=[], decisions=[], next_steps=[], excluded=[])),
                                 usage={"prompt_tokens": 77, "completion_tokens": 12})

    builder = ModelAnchorBuilder(Model(), provider_max_output_tokens=32_768)
    builder({"constraints": {"max_tokens": semantic_limit}, "new_turns": []})
    assert calls[0]["max_tokens"] == expected
    assert builder.take_calls()[0].output_token_count == 12
