"""SkillOptimizer — use_skill 后失败信号 → 小 diff patch + 备份（8.04 Phase 3.3）。

不经 LLM：把失败 tip 追加到「失败处理」节；旧版写 SKILL.md.bak.<n>。
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..memory.types import TurnEnvelope
from .types import EvolutionSignal

logger = logging.getLogger(__name__)


def _next_bak_path(skill_path: Path) -> Path:
    n = 1
    while True:
        bak = skill_path.with_name(f"SKILL.md.bak.{n}")
        if not bak.exists():
            return bak
        n += 1


def _append_failure_tip(text: str, tip: str) -> str:
    bullet = f"- 遇到「{tip}」时：先核对前置条件并重试一次可控步骤，仍失败则报告完整错误。"
    if "## 失败处理" in text:
        # 插到该节末尾（下一 ## 之前）
        parts = text.split("## 失败处理", 1)
        head, rest = parts[0], parts[1]
        if "\n## " in rest:
            section, tail = rest.split("\n## ", 1)
            section = section.rstrip() + "\n" + bullet + "\n"
            return head + "## 失败处理" + section + "\n## " + tail
        return head + "## 失败处理" + rest.rstrip() + "\n" + bullet + "\n"
    return text.rstrip() + "\n\n## 失败处理\n\n" + bullet + "\n"


class SkillOptimizer:
    """旁路修正器：skills_used ∩ tool_failures → patch SKILL.md。"""

    def __init__(self, registry) -> None:
        self._registry = registry

    def optimize(
        self,
        turn: TurnEnvelope,
        signals: EvolutionSignal | None,
    ) -> bool:
        if signals is None or not signals.skills_used or not signals.tool_failures:
            return False
        tip = signals.tool_failures[0].strip()
        if not tip:
            return False

        changed = False
        for name in signals.skills_used:
            skill = self._registry.get(name)
            if skill is None or not skill.path:
                continue
            path = Path(skill.path)
            if not path.is_file():
                continue
            before = path.read_text(encoding="utf-8")
            if tip in before:
                continue
            bak = _next_bak_path(path)
            bak.write_text(before, encoding="utf-8")
            after = _append_failure_tip(before, tip)
            path.write_text(after, encoding="utf-8")
            if hasattr(self._registry, "reload"):
                self._registry.reload()
            logger.info(
                "SkillOptimizer patch skill=%s bak=%s turn_id=%s",
                name, bak.name, turn.turn_id,
            )
            changed = True
        return changed
