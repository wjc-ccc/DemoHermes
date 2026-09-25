"""SkillOptimizer — 把「技能生效期间的失败」变成正文补丁提案。

不再直接改生产 SKILL.md。draft 可由 SkillEvolution 立即打补丁（尚未进 L1）；
active 必须经 shadow replay 证明无回退后，才由质量门提交。
"""
from __future__ import annotations

import logging

from ..core.memory.types import TurnRecord
from ..core.types import Session
from .turn_facts import skill_failure_attribution
from .types import EvolutionSignal, SkillPatchProposal

logger = logging.getLogger(__name__)


def _failure_bullet(tip: str) -> str:
    return (
        f"- 遇到「{tip}」时：先核对前置条件并重试一次可控步骤，"
        "仍失败则报告完整错误。"
    )


def render_failure_patch(body: str, tip: str) -> tuple[str, str] | None:
    """返回 (old_string, new_string)；正文已含该 tip 时返回 None。"""
    if not tip or tip in body:
        return None
    bullet = _failure_bullet(tip)
    if "## 失败处理" in body:
        parts = body.split("## 失败处理", 1)
        rest = parts[1]
        if "\n## " in rest:
            section, _tail = rest.split("\n## ", 1)
        else:
            section = rest
        old = "## 失败处理" + section.rstrip()
        new = old + "\n" + bullet
        if old not in body:
            return None
        return old, new
    old = body.rstrip()
    new = old + "\n\n## 失败处理\n\n" + bullet
    return old, new


class SkillOptimizer:
    """从已落库事实提出补丁，不写盘。"""

    def __init__(self, registry) -> None:
        self._registry = registry

    def propose(
        self,
        turn: TurnRecord,
        session: Session,
        signals: EvolutionSignal | None = None,
    ) -> tuple[SkillPatchProposal, ...]:
        del signals
        attribution = skill_failure_attribution(turn, session)
        proposals: list[SkillPatchProposal] = []
        for name, failures in attribution.items():
            skill = self._registry.get(name)
            if skill is None or skill.source == "builtin":
                continue
            tip = failures[0].strip()
            rendered = render_failure_patch(skill.body, tip)
            if rendered is None:
                continue
            old, new = rendered
            proposals.append(
                SkillPatchProposal(
                    skill_name=name,
                    old_string=old,
                    new_string=new,
                    turn_id=turn.turn_id,
                    rationale=f"skill_failure:{tip}",
                )
            )
        return tuple(proposals)

    def optimize(
        self,
        turn: TurnRecord,
        signals: EvolutionSignal | None,
        *,
        session: Session | None = None,
    ) -> bool:
        """兼容旧 seam：只在传入 session 时能提出补丁；默认不再写盘。"""
        if session is None:
            logger.debug("SkillOptimizer.optimize 缺少 session，跳过写盘")
            return False
        return bool(self.propose(turn, session, signals))
