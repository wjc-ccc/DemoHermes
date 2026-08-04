"""Phase 2.2–2.3：playbook harmful 加权 + refine / 每 N turn 触发。

公共 seam：
  - PlaybookCurator.record_failure(scope, tip) → 达阈值后 entry 标 [playbook][harmful]
  - PlaybookCurator.refine(scope) → 去掉 harmful / 近重复（delta remove）
  - EvolutionScheduler 每 N 次 notify 调用 refine（可注入）
"""
from __future__ import annotations

from Agent.evolution.memory_curator import PlaybookCurator
from Agent.evolution.scheduler import EvolutionScheduler
from Agent.memory import (
    BuiltinMemoryMutation,
    BuiltinMemoryReadRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)
from Agent.memory.types import TurnEnvelope

_SCOPE = MemoryScope(kind="personal", key="cli:user:p22")
_PROVENANCE = MemoryProvenance(source="reviewer", turn_id="seed")


def _turn(turn_id: str = "t1") -> TurnEnvelope:
    return TurnEnvelope(
        turn_id=turn_id,
        session_id="s1",
        scope=_SCOPE,
        user_message_id="m1",
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1.0,
        finished_at=2.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_record_failure_marks_matching_playbook_harmful_after_threshold(tmp_path):
    """同一失败 tip 连续达到阈值后，对应 playbook 被标 harmful。"""
    manager = MemoryManager(root=tmp_path / "memory")
    tip_body = "工具超时时先重试一次再报告"
    manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE,
            target="memory",
            action="add",
            content=f"[playbook] 遇到类似失败「{tip_body}」时：先重试",
            provenance=_PROVENANCE,
        )
    )
    curator = PlaybookCurator(manager, failure_threshold=3)

    for i in range(3):
        marked = curator.record_failure(_SCOPE, tip_body, turn_id=f"t{i}")

    assert marked is True
    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="memory"))
    assert any(e.content.startswith("[playbook][harmful]") for e in view.entries)


def test_refine_removes_harmful_entries(tmp_path):
    """refine：删除 harmful，保留健康 playbook。"""
    manager = MemoryManager(root=tmp_path / "memory")
    manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="memory", action="add",
            content="[playbook] 健康策略A", provenance=_PROVENANCE,
        )
    )
    manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="memory", action="add",
            content="[playbook][harmful] 过时策略B", provenance=_PROVENANCE,
        )
    )
    curator = PlaybookCurator(manager)

    removed = curator.refine(_SCOPE)

    assert removed >= 1
    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="memory"))
    contents = [e.content for e in view.entries]
    assert any("健康策略A" in c for c in contents)
    assert not any("[playbook][harmful]" in c for c in contents)


def test_scheduler_refines_every_n_turns(tmp_path):
    """Scheduler 每 N 次 notify 触发 refine（此处 N=2）。"""
    manager = MemoryManager(root=tmp_path / "memory")
    manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="memory", action="add",
            content="[playbook][harmful] 该删", provenance=_PROVENANCE,
        )
    )
    curator = PlaybookCurator(manager)
    sched = EvolutionScheduler()
    calls = {"n": 0}

    def on_refine(scope):
        calls["n"] += 1
        return curator.refine(scope)

    sched.register_periodic_refine(
        every_n=2,
        refine=on_refine,
        scope_of=lambda turn: turn.scope,
    )

    sched.notify(_turn("t1"))
    assert calls["n"] == 0  # 第 1 次未到
    sched.notify(_turn("t2"))
    assert calls["n"] == 1

    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="memory"))
    assert not any("[playbook][harmful]" in e.content for e in view.entries)
