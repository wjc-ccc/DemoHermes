"""SkillCreator — 可复用多步轨迹 → data/skills/<slug>/SKILL.md（8.04 Phase 3.1）。

规则版：成功且 ≥2 个非 use_skill 工具的序列达频率阈值后写盘。
新 Skill 默认 status=draft；不经 memory/procedural 第二套存储。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..memory.types import TurnEnvelope
from .types import EvolutionSignal, SkillProposal

logger = logging.getLogger(__name__)

_SKIP_TOOLS = frozenset({"use_skill"})


def _slugify(tools: tuple[str, ...]) -> str:
    raw = "_".join(tools)
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_").lower()
    return slug or "unnamed_skill"


def _procedural_tools(sequence: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(t for t in sequence if t and t not in _SKIP_TOOLS)


def _render_skill_md(*, name: str, description: str, steps: tuple[str, ...]) -> str:
    step_lines = "\n".join(f"{i}. 调用工具 `{t}` 并确认结果成功。" for i, t in enumerate(steps, 1))
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"status: draft\n"
        f"---\n\n"
        f"# {name}\n\n"
        f"## 目标\n\n"
        f"{description}\n\n"
        f"## 前置\n\n"
        f"- 相关工具已注册且可用：{', '.join(f'`{t}`' for t in steps)}\n\n"
        f"## 步骤\n\n"
        f"{step_lines}\n\n"
        f"## 失败处理\n\n"
        f"- 任一步失败时：记录错误、不要静默跳过，必要时向用户报告完整错误后停止。\n"
    )


def _render_proposal_md(proposal: SkillProposal) -> str:
    body = proposal.body.strip()
    if not body.startswith("#"):
        body = f"# {proposal.name}\n\n{body}"
    return (
        f"---\n"
        f"name: {proposal.name}\n"
        f"description: {proposal.description}\n"
        f"status: {proposal.status}\n"
        f"---\n\n"
        f"{body}\n"
    )


class SkillCreator:
    """旁路 Skill 创建器：consider(turn, signals) 达阈值则写 SKILL.md。"""

    def __init__(
        self,
        skills_root: Path,
        *,
        frequency_threshold: int = 2,
        registry=None,
    ) -> None:
        if frequency_threshold < 1:
            raise ValueError("frequency_threshold 必须 >= 1")
        self._root = Path(skills_root)
        self._frequency_threshold = frequency_threshold
        self._registry = registry
        self._counts: dict[str, int] = {}

    def create_from_proposal(self, proposal: SkillProposal) -> Path | None:
        """Hermes distill 落盘：已存在同名则跳过。"""
        name = re.sub(r"[^a-zA-Z0-9_-]+", "_", (proposal.name or "").strip()).strip("_")
        if not name:
            return None
        skill_dir = self._root / name
        skill_path = skill_dir / "SKILL.md"
        if skill_path.exists():
            return None
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(_render_proposal_md(proposal), encoding="utf-8")
        logger.info("SkillCreator proposal→disk skill=%s path=%s", name, skill_path)
        self._register_path(skill_path)
        return skill_path

    def consider(
        self,
        turn: TurnEnvelope,
        signals: EvolutionSignal | None,
    ) -> Path | None:
        if not turn.completed or turn.error:
            return None
        if signals is None or signals.tool_failures:
            return None
        steps = _procedural_tools(signals.tool_sequence)
        if len(steps) < 2:
            return None

        slug = _slugify(steps)
        self._counts[slug] = self._counts.get(slug, 0) + 1
        if self._counts[slug] < self._frequency_threshold:
            return None

        skill_dir = self._root / slug
        skill_path = skill_dir / "SKILL.md"
        if skill_path.exists():
            return None

        description = f"按顺序执行 {' → '.join(steps)} 的可复用流程"
        text = _render_skill_md(name=slug, description=description, steps=steps)
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(text, encoding="utf-8")
        logger.info("SkillCreator 写出 draft skill=%s path=%s", slug, skill_path)
        self._register_path(skill_path)
        return skill_path

    def _register_path(self, skill_path: Path) -> None:
        if self._registry is None:
            return
        if hasattr(self._registry, "reload"):
            self._registry.reload()
        else:
            from ..skills.base import Skill

            self._registry.register(Skill.from_file(skill_path))
