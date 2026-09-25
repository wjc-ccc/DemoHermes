"""SkillCreator — 可复用多步轨迹 → data/skills/<slug>/SKILL.md。

只负责「要不要沉淀」和「提案长什么样」；落盘一律走 SkillRepository，
因此新技能一定是 draft，且无法绕过 frontmatter 注入 / 覆盖内置技能。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..core.memory.types import TurnRecord
from ..skills.base import normalize_skill_name
from ..skills.repository import SkillChangeRequest, SkillDraft, SkillRepository
from .types import EvolutionSignal, SkillProposal

logger = logging.getLogger(__name__)

_SKIP_TOOLS = frozenset({"use_skill"})


def _procedural_tools(sequence: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(t for t in sequence if t and t not in _SKIP_TOOLS)


def _render_steps(steps: tuple[str, ...], description: str) -> str:
    step_lines = "\n".join(
        f"{i}. 调用工具 `{t}` 并确认结果成功。" for i, t in enumerate(steps, 1)
    )
    return (
        f"## 目标\n\n{description}\n\n"
        f"## 前置\n\n"
        f"- 相关工具已注册且可用：{', '.join(f'`{t}`' for t in steps)}\n\n"
        f"## 步骤\n\n{step_lines}\n\n"
        f"## 失败处理\n\n"
        f"- 任一步失败时：记录错误、不要静默跳过，必要时向用户报告完整错误后停止。\n"
    )


class SkillCreator:
    """旁路 Skill 创建器：consider(turn, signals) 达阈值则写候选 SKILL.md。"""

    def __init__(
        self,
        skills_root: Path,
        *,
        frequency_threshold: int = 2,
        registry=None,
        repository: SkillRepository | None = None,
    ) -> None:
        if frequency_threshold < 1:
            raise ValueError("frequency_threshold 必须 >= 1")
        self._root = Path(skills_root)
        self._frequency_threshold = frequency_threshold
        self._registry = registry
        self._repo = repository or SkillRepository(self._root, registry=registry)
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def repository(self) -> SkillRepository:
        return self._repo

    def create_from_proposal(self, proposal: SkillProposal) -> Path | None:
        """Hermes distill 落盘：强制 draft；同名或同正文则跳过。"""
        name = normalize_skill_name(proposal.name)
        if not name:
            return None
        result = self._repo.apply(
            SkillChangeRequest(
                action="create",
                name=name,
                draft=SkillDraft(
                    name=name,
                    description=proposal.description,
                    body=proposal.body,
                    source="generated",
                    scope_key=proposal.scope_key,
                    origin_turn_id=proposal.turn_id or "",
                    required_tools=proposal.required_tools,
                ),
                actor="skill_creator",
                reason=proposal.rationale or "proposal",
                turn_id=proposal.turn_id,
            )
        )
        if not result.ok:
            logger.info(
                "SkillCreator 跳过 proposal name=%s code=%s", name, result.code
            )
            return None
        return Path(result.path)

    def consider(
        self,
        turn: TurnRecord,
        signals: EvolutionSignal | None,
        *,
        scope_key: str = "",
    ) -> Path | None:
        if not turn.completed or turn.error:
            return None
        if signals is None or signals.tool_failures:
            return None
        steps = _procedural_tools(signals.tool_sequence)
        if len(steps) < 2:
            return None

        slug = normalize_skill_name("_".join(steps)) or "unnamed_skill"
        with self._lock:
            self._counts[slug] = self._counts.get(slug, 0) + 1
            count = self._counts[slug]
        if count < self._frequency_threshold:
            return None

        description = f"按顺序执行 {' → '.join(steps)} 的可复用流程"
        result = self._repo.apply(
            SkillChangeRequest(
                action="create",
                name=slug,
                draft=SkillDraft(
                    name=slug,
                    description=description,
                    body=_render_steps(steps, description),
                    source="generated",
                    scope_key=scope_key or turn.scope.storage_key,
                    origin_turn_id=turn.turn_id,
                ),
                actor="skill_creator",
                reason="frequency_threshold",
                turn_id=turn.turn_id,
            )
        )
        if not result.ok:
            return None
        logger.info("SkillCreator 写出 draft skill=%s path=%s", slug, result.path)
        return Path(result.path)
