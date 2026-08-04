"""Phase 1.2 / 1.5：EvolutionReflector seam + Scheduler 管线。

公共 seam：
  - EvolutionReflector.reflect(turn, signals) → EvolutionInsight[]
  - EvolutionScheduler + PlaybookCurator 串联后，manager.read 可见 playbook
"""
from __future__ import annotations

from Agent.core.types import Message
from Agent.evolution.memory_curator import PlaybookCurator
from Agent.evolution.scheduler import EvolutionScheduler
from Agent.evolution.types import EvolutionSignal
from Agent.memory import BuiltinMemoryReadRequest, MemoryManager, MemoryScope
from Agent.memory.episodic.store import SqliteSessionStore
from Agent.memory.types import TurnEnvelope

_SCOPE = MemoryScope(kind="personal", key="cli:user:ref1")


def _turn(session, *, turn_id="t1") -> TurnEnvelope:
    return TurnEnvelope(
        turn_id=turn_id,
        session_id=session.session_id,
        scope=_SCOPE,
        user_message_id=session.messages[-1].message_id,
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_rule_reflector_emits_playbook_on_tool_failure(tmp_path):
    """工具失败信号 → Reflector 产出带 [playbook] 前缀的 add insight。"""
    from Agent.evolution.reviewer import RuleBasedEvolutionReflector

    store = SqliteSessionStore(tmp_path / "memory.db")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="部署到生产"))
    turn = _turn(session)
    signals = EvolutionSignal(
        turn_id=turn.turn_id,
        completed=True,
        tool_failures=("bash: timeout",),
    )

    insights = RuleBasedEvolutionReflector(store).reflect(turn, signals)

    assert len(insights) == 1
    assert insights[0].op == "add"
    assert insights[0].content.startswith("[playbook]")
    assert "timeout" in insights[0].content.lower() or "超时" in insights[0].content


def test_rule_reflector_silent_when_no_failure_signal(tmp_path):
    """无失败信号时 Reflector 不产出 insight（避免刷屏）。"""
    from Agent.evolution.reviewer import RuleBasedEvolutionReflector

    store = SqliteSessionStore(tmp_path / "memory.db")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="你好"))
    turn = _turn(session)

    insights = RuleBasedEvolutionReflector(store).reflect(
        turn, EvolutionSignal(turn_id=turn.turn_id, completed=True),
    )
    assert insights == []


def test_scheduler_pipeline_writes_playbook_after_notify(tmp_path):
    """Scheduler handler：Reflector → Curator；notify 后 MEMORY 可读。"""
    from Agent.evolution.reviewer import RuleBasedEvolutionReflector

    store = SqliteSessionStore(tmp_path / "memory.db")
    manager = MemoryManager(root=tmp_path / "memory")
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="跑测试"))
    turn = _turn(session, turn_id="t_pipe")

    reflector = RuleBasedEvolutionReflector(store)
    curator = PlaybookCurator(manager)
    signals = EvolutionSignal(
        turn_id=turn.turn_id,
        tool_failures=("pytest: failed",),
    )

    sched = EvolutionScheduler()

    def _evolve(t):
        curator.apply_insights(reflector.reflect(t, signals))

    sched.register(_evolve)
    sched.notify(turn)

    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="memory"))
    assert any("[playbook]" in e.content for e in view.entries)
