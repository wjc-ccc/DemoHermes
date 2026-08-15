"""
EvolutionScheduler — 进化任务调度

管理进化流程的触发时机：
    - 回合结束后立即触发 reviewer（同步）
    - 每 N turn 触发 playbook refine（Phase 2.3）
    - 批量 skill_optimizer 优化（低优先级后台，后置）

避免进化任务阻塞主对话循环。

Phase 0.3（8.04）：最小扇出 — register/notify；单个 handler 失败不阻断兄弟。
Phase 2.3：register_periodic_refine(every_n, refine, scope_of)
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ..memory.types import MemoryScope, TurnRecord

logger = logging.getLogger(__name__)

TurnHandler = Callable[[TurnRecord], Any]
RefineFn = Callable[[MemoryScope], Any]
ScopeOfFn = Callable[[TurnRecord], MemoryScope]


class EvolutionScheduler:
    """旁路调度：commit 后 hook 调用 notify，扇出到已注册 handler。"""

    def __init__(self) -> None:
        self._handlers: list[TurnHandler] = []
        self._turn_count = 0
        self._refine_every_n: int | None = None
        self._refine: RefineFn | None = None
        self._scope_of: ScopeOfFn | None = None

    def register(self, handler: TurnHandler) -> None:
        self._handlers.append(handler)

    def register_periodic_refine(
        self,
        *,
        every_n: int,
        refine: RefineFn,
        scope_of: ScopeOfFn,
    ) -> None:
        """每 every_n 次 notify 调用 refine(scope_of(turn))。"""
        if every_n < 1:
            raise ValueError("every_n 必须 >= 1")
        self._refine_every_n = every_n
        self._refine = refine
        self._scope_of = scope_of

    def notify(self, turn: TurnRecord) -> None:
        for handler in self._handlers:
            try:
                handler(turn)
            except Exception:
                logger.exception(
                    "evolution handler 失败 turn_id=%s", turn.turn_id
                )
        self._turn_count += 1
        if (
            self._refine_every_n is not None
            and self._refine is not None
            and self._scope_of is not None
            and self._turn_count % self._refine_every_n == 0
        ):
            try:
                self._refine(self._scope_of(turn))
            except Exception:
                logger.exception(
                    "periodic refine 失败 turn_id=%s", turn.turn_id
                )
