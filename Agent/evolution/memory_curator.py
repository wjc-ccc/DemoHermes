"""PlaybookCurator — 把 EvolutionInsight 增量写入 MEMORY.md（8.04 Phase 1.3 / Phase 2）。

禁止默认整文件 rewrite；只经 MemoryManager.apply 做 add/replace/remove。
写入前走 EvolutionCommitGate。

Phase 2.2–2.3：
  - record_failure：同类失败达阈值 → 将匹配 playbook 标为 [playbook][harmful]
  - refine：delta 删除 harmful（及防御性近重复）
"""
from __future__ import annotations

import logging
import unicodedata
from collections import defaultdict
from collections.abc import Sequence

from ..memory import (
    BuiltinMemoryMutation,
    BuiltinMemoryReadRequest,
    MemoryManager,
    MemoryMutationResult,
    MemoryProvenance,
    MemoryScope,
)
from .compactor import HARMFUL_MARK, PLAYBOOK_MARK
from .gate import EvolutionCommitGate
from .types import EvolutionInsight

logger = logging.getLogger(__name__)


def _normalize(content: str) -> str:
    return unicodedata.normalize("NFKC", content).casefold().strip()


class PlaybookCurator:
    """ACE Curator：insight → delta mutation → Manager.apply。"""

    def __init__(
        self,
        manager: MemoryManager,
        gate: EvolutionCommitGate | None = None,
        *,
        failure_threshold: int = 3,
    ) -> None:
        self._manager = manager
        self._gate = gate or EvolutionCommitGate()
        self._failure_threshold = max(1, failure_threshold)
        self._failure_counts: dict[tuple[str, str, str], int] = defaultdict(int)

    def apply_insights(
        self, insights: Sequence[EvolutionInsight],
    ) -> list[MemoryMutationResult]:
        results: list[MemoryMutationResult] = []
        for insight in insights:
            result = self._apply_one(insight)
            if result is not None:
                results.append(result)
        return results

    def record_failure(
        self,
        scope: MemoryScope,
        tip: str,
        *,
        turn_id: str | None = None,
    ) -> bool:
        """累计同类失败；达阈值后把正文含 tip 的 playbook 标 harmful。

        返回是否在本次调用中完成了 harmful 标记。
        """
        tip_n = _normalize(tip)
        if not tip_n:
            return False
        key = (scope.kind, scope.key, tip_n)
        self._failure_counts[key] += 1
        if self._failure_counts[key] < self._failure_threshold:
            return False

        view = self._manager.read(
            BuiltinMemoryReadRequest(scope=scope, target="memory")
        )
        for entry in view.entries:
            if tip_n not in _normalize(entry.content):
                continue
            if not entry.content.lstrip().startswith(PLAYBOOK_MARK):
                continue
            if entry.content.lstrip().startswith(HARMFUL_MARK):
                return True  # 已标记
            # 升级前缀：[playbook] → [playbook][harmful]
            body = entry.content
            if body.lstrip().startswith(HARMFUL_MARK):
                new_content = body
            elif body.lstrip().startswith(PLAYBOOK_MARK):
                # 保留原文，仅插入 harmful 标记
                stripped = body.lstrip()
                rest = stripped[len(PLAYBOOK_MARK):].lstrip()
                new_content = f"{HARMFUL_MARK} {rest}".rstrip()
            else:
                new_content = f"{HARMFUL_MARK} {body.strip()}"
            result = self._manager.apply(
                BuiltinMemoryMutation(
                    scope=scope,
                    target="memory",
                    action="replace",
                    entry_id=entry.entry_id,
                    content=new_content,
                    provenance=MemoryProvenance(
                        source="reviewer", turn_id=turn_id,
                    ),
                )
            )
            return bool(result.ok)
        return False

    def refine(self, scope: MemoryScope) -> int:
        """grow-and-refine 的 delta 面：删除 harmful 与近重复，返回删除条数。"""
        view = self._manager.read(
            BuiltinMemoryReadRequest(scope=scope, target="memory")
        )
        removed = 0
        seen: set[str] = set()
        for entry in view.entries:
            content = entry.content
            norm = _normalize(content)
            drop = False
            if content.lstrip().startswith(HARMFUL_MARK):
                drop = True
            elif norm in seen:
                drop = True
            else:
                seen.add(norm)
            if not drop:
                continue
            result = self._manager.apply(
                BuiltinMemoryMutation(
                    scope=scope,
                    target="memory",
                    action="remove",
                    entry_id=entry.entry_id,
                    provenance=MemoryProvenance(source="reviewer"),
                )
            )
            if result.ok:
                removed += 1
        return removed

    def _apply_one(self, insight: EvolutionInsight) -> MemoryMutationResult | None:
        view = self._manager.read(
            BuiltinMemoryReadRequest(scope=insight.scope, target="memory")
        )
        existing = tuple(e.content for e in view.entries)
        current_chars = len(view.rendered or "")
        decision = self._gate.admit(
            insight,
            existing_contents=existing,
            current_memory_chars=current_chars,
        )
        if not decision.admitted:
            logger.info(
                "playbook insight 被拒绝 reason=%s turn_id=%s",
                decision.reason, insight.turn_id,
            )
            return None

        provenance = MemoryProvenance(
            source="reviewer",
            turn_id=insight.turn_id,
        )
        if insight.op == "add":
            mutation = BuiltinMemoryMutation(
                scope=insight.scope,
                target="memory",
                action="add",
                content=insight.content,
                provenance=provenance,
            )
        elif insight.op == "replace":
            mutation = BuiltinMemoryMutation(
                scope=insight.scope,
                target="memory",
                action="replace",
                entry_id=insight.entry_id,
                content=insight.content,
                provenance=provenance,
            )
        else:
            mutation = BuiltinMemoryMutation(
                scope=insight.scope,
                target="memory",
                action="remove",
                entry_id=insight.entry_id,
                provenance=provenance,
            )

        result = self._manager.apply(mutation)
        if not result.ok:
            logger.warning(
                "playbook apply 失败 error=%s turn_id=%s",
                result.error_code or result.message, insight.turn_id,
            )
        return result
