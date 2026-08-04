"""Evolution 公共类型（8.04 Phase 1）。

EvolutionInsight：Reflector 产出的增量策略建议（非 MemoryMutation）。
GateDecision：CommitGate 是否接纳该 insight。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..memory.types import MemoryScope

EvolutionOp = Literal["add", "replace", "remove"]


class EvolutionInsight(BaseModel):
    """一条可策展的策略建议；Curator 负责转成 MemoryMutation。"""

    model_config = ConfigDict(frozen=True)

    scope: MemoryScope
    op: EvolutionOp
    content: str | None = None
    entry_id: str | None = None
    rationale: str = ""
    kind: Literal["playbook"] = "playbook"
    turn_id: str | None = None


class GateDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    admitted: bool
    reason: str = ""


class EvolutionSignal(BaseModel):
    """轻量 turn 信号（Phase 0.4 / Reflector 输入）；可缺省。"""

    model_config = ConfigDict(frozen=True)

    turn_id: str
    completed: bool = True
    interrupted: bool = False
    error: str | None = None
    iteration_count: int = 0
    tool_failures: tuple[str, ...] = Field(default_factory=tuple)
    # Phase 3：按执行顺序的工具名（含 use_skill）；供 SkillCreator / Optimizer
    tool_sequence: tuple[str, ...] = Field(default_factory=tuple)
    skills_used: tuple[str, ...] = Field(default_factory=tuple)
