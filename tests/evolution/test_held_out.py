"""Phase 5.1：Held-out 选择 — 候选 playbook 不降级才 commit。

公共 seam：
  - HeldOutSelector.admit(insight, existing_contents) → GateDecision
  - 固定任务集：required_tips 假想应用后仍在；forbidden 不得出现在新内容
"""
from __future__ import annotations

from Agent.evolution.held_out import HeldOutSelector, HeldOutTask
from Agent.evolution.types import EvolutionInsight
from Agent.memory.types import MemoryScope

_SCOPE = MemoryScope(kind="personal", key="cli:user:ho1")


def _insight(content: str, *, op: str = "add") -> EvolutionInsight:
    return EvolutionInsight(
        scope=_SCOPE, op=op, content=content, rationale="test", turn_id="t1",
    )


def test_held_out_rejects_forbidden_regression():
    """新 insight 含 forbidden 模式 → 拒绝（防越进化越差）。"""
    selector = HeldOutSelector(
        tasks=(
            HeldOutTask(
                name="timeout_retry",
                required_tips=("[playbook] 超时先重试",),
                forbidden_in_new=("静默跳过错误",),
            ),
        )
    )
    decision = selector.admit(
        _insight("[playbook] 出错时静默跳过错误继续"),
        existing_contents=("[playbook] 超时先重试",),
    )
    assert decision.admitted is False
    assert decision.reason == "held_out_forbidden"


def test_held_out_rejects_when_required_tip_would_be_removed():
    """remove 会删掉 held-out required tip → 拒绝。"""
    selector = HeldOutSelector(
        tasks=(
            HeldOutTask(
                name="keep_retry",
                required_tips=("[playbook] 超时先重试",),
            ),
        )
    )
    decision = selector.admit(
        EvolutionInsight(
            scope=_SCOPE,
            op="remove",
            entry_id="e1",
            content="[playbook] 超时先重试",
            rationale="bad",
        ),
        existing_contents=("[playbook] 超时先重试",),
    )
    assert decision.admitted is False
    assert decision.reason == "held_out_required_missing"


def test_held_out_admits_safe_add():
    """安全增量不影响 required → 接纳。"""
    selector = HeldOutSelector(
        tasks=(
            HeldOutTask(
                name="keep_retry",
                required_tips=("[playbook] 超时先重试",),
                forbidden_in_new=("静默跳过",),
            ),
        )
    )
    decision = selector.admit(
        _insight("[playbook] 工具失败时向用户报告完整错误"),
        existing_contents=("[playbook] 超时先重试",),
    )
    assert decision.admitted is True
    assert decision.reason == "ok"
