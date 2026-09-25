"""确定性 turn 事实：进化只读已落库 ToolCall，不赌 EventBus。"""
from __future__ import annotations

from agent.core.types import Session, ToolCall
from agent.evolution.turn_facts import build_turn_facts, skill_failure_attribution
from agent.core.memory import MemoryScope
from agent.core.memory.types import TurnRecord


def _turn() -> TurnRecord:
    return TurnRecord(
        turn_id="t1",
        session_id="s1",
        scope=MemoryScope(tenant_id="cli", user_id="u"),
        user_message_id="m",
        assistant_message_ids=("a1",),
        tool_call_ids=("c1", "c2", "c3"),
        started_at=1.0,
        finished_at=2.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_build_turn_facts_from_committed_tool_calls():
    session = Session(
        session_id="s1",
        tool_calls=[
            ToolCall(
                tool_call_id="c1",
                trajectory_id="t1",
                tool_name="use_skill",
                arguments={"skill_name": "weekly_report"},
                status="success",
                tool_call_result={"skill_name": "weekly_report", "skill_revision": 1},
                created_at=1.0,
            ),
            ToolCall(
                tool_call_id="c2",
                trajectory_id="t1",
                tool_name="calculator",
                status="error",
                error="boom",
                created_at=2.0,
            ),
            ToolCall(
                tool_call_id="c3",
                trajectory_id="t1",
                tool_name="calculator",
                status="success",
                created_at=3.0,
            ),
        ],
    )
    facts = build_turn_facts(_turn(), session)
    assert facts.tool_sequence == ("use_skill", "calculator", "calculator")
    assert facts.skills_used == ("weekly_report",)
    assert facts.tool_failures == ("calculator: boom",)
    assert facts.recovered_from_failure is True


def test_failure_is_attributed_only_to_the_active_skill():
    session = Session(
        session_id="s1",
        tool_calls=[
            ToolCall(
                tool_call_id="c1",
                trajectory_id="t1",
                tool_name="use_skill",
                arguments={"skill_name": "alpha"},
                status="success",
                tool_call_result={"skill_name": "alpha"},
                created_at=1.0,
            ),
            ToolCall(
                tool_call_id="c2",
                trajectory_id="t1",
                tool_name="use_skill",
                arguments={"skill_name": "beta"},
                status="success",
                tool_call_result={"skill_name": "beta"},
                created_at=2.0,
            ),
            ToolCall(
                tool_call_id="c3",
                trajectory_id="t1",
                tool_name="calculator",
                status="error",
                error="boom",
                created_at=3.0,
            ),
        ],
    )
    attributed = skill_failure_attribution(_turn(), session)
    assert attributed == {"beta": ("calculator: boom",)}
    assert "alpha" not in attributed
