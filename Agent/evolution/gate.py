"""EvolutionCommitGate — playbook 写入前门控（8.04 Phase 1.4）。

v0：空 insight / 近重复 / 超 MEMORY 字符预算 → 拒绝。
不写盘；只做接纳判定。
"""
from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from ..memory.budget import MEMORY_MAX_CHARS
from .types import EvolutionInsight, GateDecision

PLAYBOOK_PREFIX = "[playbook]"


def _normalize(content: str) -> str:
    return unicodedata.normalize("NFKC", content).casefold().strip()


class EvolutionCommitGate:
    """增量 playbook 提交门控（RSEA 思想的最小实现）。"""

    def __init__(self, *, max_memory_chars: int = MEMORY_MAX_CHARS) -> None:
        self._max_memory_chars = max_memory_chars

    def admit(
        self,
        insight: EvolutionInsight,
        *,
        existing_contents: Sequence[str],
        current_memory_chars: int,
    ) -> GateDecision:
        if insight.op == "add":
            content = (insight.content or "").strip()
            if not content:
                return GateDecision(admitted=False, reason="empty_insight")
            norm = _normalize(content)
            for existing in existing_contents:
                if _normalize(existing) == norm:
                    return GateDecision(admitted=False, reason="near_duplicate")
            # 粗估：当前文档字符 + 新 content（围栏开销忽略，偏保守用 content 长度）
            if current_memory_chars + len(content) > self._max_memory_chars:
                return GateDecision(admitted=False, reason="over_budget")
            return GateDecision(admitted=True, reason="ok")

        if insight.op == "replace":
            content = (insight.content or "").strip()
            if not content or not insight.entry_id:
                return GateDecision(admitted=False, reason="empty_insight")
            return GateDecision(admitted=True, reason="ok")

        if insight.op == "remove":
            if not insight.entry_id:
                return GateDecision(admitted=False, reason="empty_insight")
            return GateDecision(admitted=True, reason="ok")

        return GateDecision(admitted=False, reason="empty_insight")
