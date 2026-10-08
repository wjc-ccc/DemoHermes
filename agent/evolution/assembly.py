"""Skill 上下文装配策略与旁路观测（8.04 Phase 4.1）。"""
from __future__ import annotations

import threading

from pydantic import BaseModel, ConfigDict


class AssemblyPolicy(BaseModel):
    """可配置 Skill 摘要预算；Memory 由 MemoryContextLoader 独立冻结。"""

    model_config = ConfigDict(frozen=True)

    # ContextBudget 步骤 4：缩短 Skill 摘要的最小字符配额；None=不启用该步
    skill_summary_min_chars: int | None = 120
    # Action 策略随完整 AssemblyPolicy 传入 Loop，不拆为零散策略 kwargs。
    toolset: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    segment_id: str | None = None


class AssemblyChoice(BaseModel):
    """单 turn 实际装配选择（观测/进化审计用）。"""

    model_config = ConfigDict(frozen=True)

    turn_id: str | None = None
    skill_summary_chars: int = 0
    skill_summary_min_chars: int | None = None


class AssemblyRecorder:
    """线程安全地记录最近装配选择。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: AssemblyChoice | None = None
        self._history: list[AssemblyChoice] = []

    def record(self, choice: AssemblyChoice) -> None:
        with self._lock:
            self._latest = choice
            self._history.append(choice)

    def latest(self) -> AssemblyChoice | None:
        with self._lock:
            return self._latest

    def history(self) -> tuple[AssemblyChoice, ...]:
        with self._lock:
            return tuple(self._history)
