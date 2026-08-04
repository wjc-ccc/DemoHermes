"""Phase 1：EvolutionReflector → Gate → PlaybookCurator（8.04 ACE 最小闭环）。

公共 seam：
  - EvolutionReflector.reflect(turn, signals) → EvolutionInsight[]
  - EvolutionCommitGate.admit(insight, ...) → GateDecision
  - PlaybookCurator.apply_insights(...) → 经 Manager.apply；用 manager.read 观测
不 mock Manager；Fake 仅用于 Reflector（外部 LLM seam）。
"""
from __future__ import annotations

from Agent.memory import (
    BuiltinMemoryMutation,
    BuiltinMemoryReadRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)
from Agent.memory.budget import MEMORY_MAX_CHARS
from Agent.memory.types import TurnEnvelope

_SCOPE = MemoryScope(kind="personal", key="cli:user:evo1")


def _turn(turn_id: str = "t1") -> TurnEnvelope:
    return TurnEnvelope(
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


def test_gate_rejects_empty_insight():
    """空 content 的 add insight 被 Gate 拒绝。"""
    from Agent.evolution.gate import EvolutionCommitGate
    from Agent.evolution.types import EvolutionInsight

    gate = EvolutionCommitGate()
    insight = EvolutionInsight(
        scope=_SCOPE, op="add", content="   ", rationale="empty",
    )
    decision = gate.admit(
        insight, existing_contents=(), current_memory_chars=10,
    )
    assert decision.admitted is False
    assert decision.reason == "empty_insight"


def test_gate_rejects_near_duplicate():
    """与已有条目近重复的 insight 被拒绝（防 collapse / 刷屏）。"""
    from Agent.evolution.gate import EvolutionCommitGate
    from Agent.evolution.types import EvolutionInsight

    gate = EvolutionCommitGate()
    insight = EvolutionInsight(
        scope=_SCOPE,
        op="add",
        content="[playbook] 超时后先重试一次再报告",
        rationale="dup",
    )
    decision = gate.admit(
        insight,
        existing_contents=("[playbook] 超时后先重试一次再报告",),
        current_memory_chars=100,
    )
    assert decision.admitted is False
    assert decision.reason == "near_duplicate"


def test_gate_rejects_over_budget():
    """写入后会超过 MEMORY 字符硬上限时拒绝。"""
    from Agent.evolution.gate import EvolutionCommitGate
    from Agent.evolution.types import EvolutionInsight

    gate = EvolutionCommitGate()
    content = "[playbook] " + ("x" * 100)
    insight = EvolutionInsight(
        scope=_SCOPE, op="add", content=content, rationale="big",
    )
    decision = gate.admit(
        insight,
        existing_contents=(),
        current_memory_chars=MEMORY_MAX_CHARS - 10,
    )
    assert decision.admitted is False
    assert decision.reason == "over_budget"


def test_curator_applies_admitted_playbook_via_manager(tmp_path):
    """Curator 把 admitted insight 写成 MEMORY playbook entry，可读回。"""
    from Agent.evolution.memory_curator import PlaybookCurator
    from Agent.evolution.types import EvolutionInsight

    manager = MemoryManager(root=tmp_path / "memory")
    curator = PlaybookCurator(manager)
    insight = EvolutionInsight(
        scope=_SCOPE,
        op="add",
        content="[playbook] 工具超时时先重试一次再向用户报告",
        rationale="from failure",
        turn_id="t9",
    )

    results = curator.apply_insights([insight])

    assert len(results) == 1
    assert results[0].ok is True
    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="memory"))
    assert any(
        "[playbook] 工具超时时先重试一次再向用户报告" in e.content
        for e in view.entries
    )


def test_curator_skips_rejected_insights(tmp_path):
    """Gate 拒绝的 insight 不会写入 Manager。"""
    from Agent.evolution.memory_curator import PlaybookCurator
    from Agent.evolution.types import EvolutionInsight

    manager = MemoryManager(root=tmp_path / "memory")
    # 先写入一条，使后续近重复被拒
    manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE,
            target="memory",
            action="add",
            content="[playbook] 已有策略",
            provenance=MemoryProvenance(source="reviewer", turn_id="seed"),
        )
    )
    curator = PlaybookCurator(manager)
    insight = EvolutionInsight(
        scope=_SCOPE,
        op="add",
        content="[playbook] 已有策略",
        rationale="dup",
        turn_id="t2",
    )

    results = curator.apply_insights([insight])

    assert results == []
    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="memory"))
    assert len(view.entries) == 1


def test_fake_reflector_to_curator_appears_in_next_snapshot(tmp_path):
    """E2E：FakeReflector insight → Curator → 下一 turn build_context 含 playbook。"""
    from Agent.evolution.memory_curator import PlaybookCurator
    from Agent.evolution.types import EvolutionInsight

    class _FakeReflector:
        def reflect(self, turn, signals=None):
            return [
                EvolutionInsight(
                    scope=turn.scope,
                    op="add",
                    content="[playbook] 部署前先跑 pytest",
                    rationale="repeat failure",
                    turn_id=turn.turn_id,
                )
            ]

    manager = MemoryManager(root=tmp_path / "memory")
    curator = PlaybookCurator(manager)
    insights = _FakeReflector().reflect(_turn("t_e2e"))
    curator.apply_insights(insights)

    snap = manager.build_context(scope=_SCOPE, query="部署", session_id="s1")
    assert "部署前先跑 pytest" in snap.prompt
    assert "<agent-memory>" in snap.prompt or "MEMORY" in snap.prompt or "playbook" in snap.prompt.lower() or "部署前先跑 pytest" in snap.prompt
