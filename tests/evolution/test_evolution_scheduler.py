"""Phase 0：EvolutionScheduler 旁路扇出（8.04 方案 §4 Phase 0.3）。

公共 seam：EvolutionScheduler.register / notify。
handler 异常不得阻断其他 handler；notify 同步扇出（主路径 hook 内调用，handler 自行异步）。
"""
from __future__ import annotations

import pytest

from agent.core.memory import MemoryScope
from agent.core.memory.types import TurnRecord


_SCOPE = MemoryScope(tenant_id="cli", user_id="u1")


def _turn(turn_id: str = "t1") -> TurnRecord:
    return TurnRecord(
        turn_id=turn_id,
        session_id="s1",
        scope=_SCOPE,
        user_message_id="m_user",
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_scheduler_notify_fans_out_to_registered_handlers():
    """notify 把同一 TurnRecord 交给所有已注册 handler。"""
    from agent.evolution.scheduler import EvolutionScheduler

    seen: list[str] = []
    sched = EvolutionScheduler()
    sched.register(lambda t: seen.append(f"a:{t.turn_id}"))
    sched.register(lambda t: seen.append(f"b:{t.turn_id}"))

    sched.notify(_turn("t42"))

    assert seen == ["a:t42", "b:t42"]


def test_scheduler_handler_failure_does_not_block_siblings():
    """单个 handler 抛错被吞掉，后续 handler 仍执行。"""
    from agent.evolution.scheduler import EvolutionScheduler

    seen: list[str] = []
    sched = EvolutionScheduler()

    def boom(_t):
        raise RuntimeError("handler boom")

    sched.register(boom)
    sched.register(lambda t: seen.append(t.turn_id))

    sched.notify(_turn("t7"))

    assert seen == ["t7"]
