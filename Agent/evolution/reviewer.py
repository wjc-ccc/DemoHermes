"""EvolutionReflector — 回合后策略复盘（8.04 Phase 1.2）。

与 TurnReviewer（事实抽取）职责分离：本模块只产出 EvolutionInsight（策略），
由 PlaybookCurator + Gate 写入 MEMORY playbook。

模型抽取是外部 seam；生产可先用 RuleBasedEvolutionReflector。
"""
from __future__ import annotations

import logging
from typing import Protocol

from ..memory.types import TurnRecord
from .types import EvolutionInsight, EvolutionSignal

logger = logging.getLogger(__name__)

PLAYBOOK_PREFIX = "[playbook]"


class EvolutionReflector(Protocol):
    """Reflector seam：turn + 可选信号 → 零个或多个策略 insight。"""

    def reflect(
        self,
        turn: TurnRecord,
        signals: EvolutionSignal | None = None,
    ) -> list[EvolutionInsight]: ...


class RuleBasedEvolutionReflector:
    """规则 Reflector：仅在有 tool_failures / turn.error 时产出避坑 playbook。

    store 预留读取用户消息（与 RuleBasedExtractor 对齐）；v0 主要看 signals。
    """

    def __init__(self, store=None) -> None:
        self._store = store

    def reflect(
        self,
        turn: TurnRecord,
        signals: EvolutionSignal | None = None,
    ) -> list[EvolutionInsight]:
        failures: list[str] = []
        if signals is not None:
            failures.extend(signals.tool_failures)
            if signals.error:
                failures.append(signals.error)
        if turn.error:
            failures.append(turn.error)
        if not failures:
            return []

        # 取第一条失败信号生成可复用避坑策略（具体、可执行，避免 brevity bias）
        tip = failures[0].strip()
        content = (
            f"{PLAYBOOK_PREFIX} 遇到类似失败「{tip}」时："
            f"先定位原因并重试一次可控步骤，仍失败则向用户报告完整错误，不要静默跳过。"
        )
        return [
            EvolutionInsight(
                scope=turn.scope,
                op="add",
                content=content,
                rationale="tool_or_turn_failure",
                turn_id=turn.turn_id,
            )
        ]
