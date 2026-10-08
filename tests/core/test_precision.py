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


def _snapshot(*, experience: str | None = None):
    return SimpleNamespace(
        prompt="用户手册",
        experience_prompt=experience,
    )


def test_load_grade_build_includes_experience_and_preserves_latest_user():
    session = _session(
        ("user", "旧问"),
        ("assistant", "旧答"),
        ("user", "新问"),
    )
    result = PrecisionBuilder().build(
        session,
        memory_snapshot=_snapshot(experience="经验卡片：先运行测试"),
        token_estimator=_PerMessage(),
        trace_id="t1",
    )
    text = "\n".join(str(message.get("content")) for message in result.messages)
    assert "经验卡片：先运行测试" in text
    assert "新问" in text
    assert result.exposed_refs == ()
    assert result.metrics.p3_exposed_count == 0
    assert any(
        record.priority == Priority.P0 and record.item_name.endswith(":seq:2")
        for record in result.records
    )
    assert any(
        record.item_name == "memory_experience"
        and record.priority == Priority.P1
        and record.decision == "selected"
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


def test_trace_counts_one_real_turn_across_react_rebuilds():
    trace = PrecisionTrace(window=20)
    engine = PrecisionBuilder(trace=trace)
    session = _session(("user", "now"))
    snapshot = _snapshot(experience="一次旧经验")
    engine.build(session, memory_snapshot=snapshot, token_estimator=_PerMessage(), trace_id="hit1")
    session.add_message(Message(role="assistant", content_text="再想一步"))
    engine.build(session, memory_snapshot=snapshot, token_estimator=_PerMessage(), trace_id="hit1")
    assert trace.record_memory_read("hit1", {"reference": "card:old"}) == ()
    snap = trace.snapshot()
    assert snap.n == 1
    assert snap.p3_hit_rate == 0.0
    assert trace.latest().trace_id == "hit1"


def test_ordinary_memory_search_does_not_create_a_lazy_handle():
    trace = PrecisionTrace()
    result = PrecisionBuilder(trace=trace).build(
        _session(("user", "now")),
        memory_snapshot=_snapshot(experience="经验卡片：正文"),
        token_estimator=_PerMessage(),
        trace_id="experience-turn",
    )
    assert result.exposed_refs == ()
    assert trace.record_memory_read(
        "experience-turn", {"reference": "card:anything"}
    ) == ()
