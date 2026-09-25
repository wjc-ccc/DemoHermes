"""Precision 唯一 interface：加载 → 分级 → 构建。"""
from types import SimpleNamespace

import pytest

from agent.core.context_budget import ContextBudget, ContextBudgetExceededError
from agent.core.precision import PrecisionBuilder, PrecisionTrace
from agent.core.precision.percision_anchor import render_anchor
from agent.core.precision.percision_types import Priority
from agent.core.types import Message, Session
from tests.anchor_fakes import DeterministicAnchorBuilder


class _PerMessage:
    def __init__(self, n: int = 1):
        self.n = n

    def estimate_messages(self, messages) -> int:
        return self.n * len(messages)


def _session(*pairs: tuple[str, str]) -> Session:
    session = Session(session_id="s")
    for role, content in pairs:
        session.add_message(Message(role=role, content_text=content))
    return session


def _snapshot(*, daily: str | None = None, provider: str | None = None):
    return SimpleNamespace(
        prompt="HOT",
        hot_prompt="HOT",
        daily_prompt=daily,
        provider_prompt=provider,
    )


def test_load_grade_build_defers_p3_and_preserves_latest_user():
    session = _session(
        ("user", "旧问"),
        ("assistant", "旧答"),
        ("user", "新问"),
    )
    result = PrecisionBuilder().build(
        session,
        memory_snapshot=_snapshot(
            daily='<today-daily date="2026-07-23">流水正文</today-daily>'
        ),
        token_estimator=_PerMessage(),
        trace_id="t1",
    )
    text = "\n".join(str(message.get("content")) for message in result.messages)
    assert "流水正文" not in text
    assert "daily:2026-07-23" in text
    assert "新问" in text
    assert result.exposed_refs == ("daily:2026-07-23",)
    assert result.metrics.p3_exposed_count == 1
    assert any(
        record.priority == Priority.P0 and record.item_name.endswith(":seq:2")
        for record in result.records
    )
    assert any(
        record.item_name == "memory_daily"
        and record.priority == Priority.P3
        and record.decision == "lazyload"
        for record in result.records
    )


def test_required_context_overflow_stops_before_model_call():
    session = _session(("user", "必须保留"))
    with pytest.raises(ContextBudgetExceededError):
        PrecisionBuilder().build(
            session,
            budget=ContextBudget(model_context_tokens=50, reserved_output_tokens=10),
            token_estimator=_PerMessage(n=100),
            trace_id="t2",
        )


def test_p2_keeps_newer_protocol_group_together_and_original_order():
    session = Session(session_id="s")
    session.add_message(Message(role="user", content_text="u1"))
    session.add_message(Message(role="assistant", content_text="a1"))
    session.add_message(Message(role="user", content_text="u2"))
    session.add_message(Message(role="assistant", content_text="a2"))
    session.add_message(Message(role="user", content_text="u3"))
    session.add_message(Message(role="assistant", content_text="a3"))
    session.add_message(Message(role="user", content_text="now"))
    result = PrecisionBuilder(anchor_builder=DeterministicAnchorBuilder()).build(
        session,
        budget=ContextBudget(model_context_tokens=18, reserved_output_tokens=10),
        token_estimator=_PerMessage(),
        trace_id="t3",
    )
    contents = [message.get("content") for message in result.messages]
    assert "u2" in contents and "a2" in contents and "u3" in contents and "a3" in contents
    assert "u1" not in contents and "a1" not in contents
    assert [message["role"] for message in result.messages] == [
        "system", "system", "user", "assistant", "user", "assistant", "user"
    ]


def test_old_tool_error_moves_to_failed_anchor_decision():
    session = Session(session_id="s")
    session.add_message(Message(role="user", content_text="旧问题"))
    session.add_message(Message(
        role="tool",
        content_text="工具执行失败: boom",
        metadata={"tool_call_id": "c"},
    ))
    session.add_message(Message(role="user", content_text="中间问题"))
    session.add_message(Message(role="assistant", content_text="中间回答"))
    session.add_message(Message(role="user", content_text="最近问题"))
    session.add_message(Message(role="assistant", content_text="最近回答"))
    session.add_message(Message(role="user", content_text="现在"))
    result = PrecisionBuilder(anchor_builder=DeterministicAnchorBuilder()).build(
        session,
        budget=ContextBudget(model_context_tokens=18, reserved_output_tokens=10),
        token_estimator=_PerMessage(),
        trace_id="t4",
    )
    assert not any(message.get("role") == "tool" for message in result.messages)
    assert "[failed] 工具执行失败: boom" in render_anchor(result.anchor)
    assert result.metrics.protected_error_missing_count == 0


def test_trace_counts_one_real_turn_across_react_rebuilds_and_records_p3_hit():
    trace = PrecisionTrace(window=20)
    engine = PrecisionBuilder(trace=trace)
    session = _session(("user", "now"))
    snapshot = _snapshot(
        daily='<today-daily date="2026-07-23">x</today-daily>'
    )
    engine.build(session, memory_snapshot=snapshot, token_estimator=_PerMessage(), trace_id="hit1")
    session.add_message(Message(role="assistant", content_text="再想一步"))
    engine.build(session, memory_snapshot=snapshot, token_estimator=_PerMessage(), trace_id="hit1")
    trace.record_memory_read(
        "hit1", {"target": "daily", "date": "2026-07-23"}
    )
    snap = trace.snapshot()
    assert snap.n == 1
    assert snap.p3_hit_rate == 1.0
    assert trace.latest().trace_id == "hit1"


def test_provider_handle_hit_matching_lives_in_trace():
    trace = PrecisionTrace()
    result = PrecisionBuilder(trace=trace).build(
        _session(("user", "now")),
        memory_snapshot=_snapshot(
            provider='<provider-memory name="fake">正文</provider-memory>'
        ),
        token_estimator=_PerMessage(),
        trace_id="provider-turn",
    )
    assert result.exposed_refs == ("provider:fake",)
    assert trace.record_memory_read(
        "provider-turn", {"query": "找 fake 档案"}
    ) == ("provider:fake",)
