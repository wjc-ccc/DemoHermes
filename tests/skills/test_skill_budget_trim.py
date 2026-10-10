"""固定 Precision 策略：Skill 属于 P0，不存在第二套独立裁剪入口。"""
from types import SimpleNamespace

import pytest

from agent.types.context import ContextBudget
from agent.core.precision.errors import ContextBudgetExceededError
from agent.core.precision.percision_engine import PrecisionBuilder
from agent.types.conversation import Message, Session


class _PerMessage:
    def __init__(self, cost: int):
        self.cost = cost

    def estimate_messages(self, messages) -> int:
        return self.cost * len(messages)


def test_skill_summary_flows_through_precision_main_path():
    session = Session(session_id="s")
    session.add_message(Message(role="user", content_text="问题"))
    selection = SimpleNamespace(summary_prompt="## Skills\n- alpha: description")
    result = PrecisionBuilder().build(
        session,
        skill_selection=selection,
        token_estimator=_PerMessage(1),
    )
    assert any(
        "alpha" in str(message.get("content")) for message in result.messages
    )


def test_required_skill_overflow_returns_context_budget_exceeded():
    session = Session(session_id="s")
    session.add_message(Message(role="user", content_text="问题"))
    selection = SimpleNamespace(summary_prompt="## Skills\n- alpha: description")
    with pytest.raises(ContextBudgetExceededError):
        PrecisionBuilder().build(
            session,
            skill_selection=selection,
            budget=ContextBudget(model_context_tokens=250, reserved_output_tokens=50),
            token_estimator=_PerMessage(100),
        )
