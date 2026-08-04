"""
SkillRegistry — 技能注册表

启动时扫描技能目录自动注册：
    agent/skills/builtin/*/SKILL.md   （内置）
    data/skills/*/SKILL.md            （运行期 Agent 自建，目录存在才扫描）

对 Loop 提供两级接口：
    summaries_prompt() : Level 1 摘要块，注入 system prompt
    load_content(name) : Level 3 完整正文，use_skill 工具调用时返回
"""
from __future__ import annotations

import logging
from pathlib import Path

from .base import Skill
from ..Provider import ROOT

logger = logging.getLogger(__name__)

# 默认扫描目录：内置技能 + 运行期自建技能
DEFAULT_SKILL_DIRS = [
    Path(__file__).resolve().parent / "builtin",
    ROOT / "data" / "skills",
]


class SkillRegistry:
    def __init__(self, skill_dirs: list[Path] | None = None) -> None:
        self._skill_dirs = list(
            skill_dirs if skill_dirs is not None else DEFAULT_SKILL_DIRS
        )
        self._skills: dict[str, Skill] = {}
        self.reload()

    # ---- 扫描注册 ----
    def reload(self) -> None:
        """清空并重新扫描全部 skill_dirs（Phase 3：Creator 写盘后热更新）。"""
        self._skills.clear()
        for directory in self._skill_dirs:
            self._scan_dir(directory)

    def _scan_dir(self, directory: Path) -> None:
        if not directory.exists():
            return
        for skill_file in sorted(directory.glob("*/SKILL.md")):
            try:
                self.register(Skill.from_file(skill_file))
            except Exception:
                logger.exception("技能文件解析失败: %s", skill_file)

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill
        logger.info("注册技能 name=%s status=%s path=%s", skill.name, skill.status, skill.path)

    # ---- 查询 ----
    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_skills(self) -> list[str]:
        return list(self._skills)

    def active_skills(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.is_active]

    # ---- 两级加载 ----
    def summaries_prompt(
        self,
        *,
        query: str | None = None,
        selector=None,
        top_k: int = 5,
    ) -> str:
        """Level 1：active 技能摘要；有 query+selector 时只注入相关子集（Hermes 式）。"""
        active = self.active_skills()
        if not active:
            return ""
        if query and selector is not None:
            active = selector.select(query, active, top_k=top_k)
            if not active:
                return ""
        lines = [skill.summary_line() for skill in active]
        return (
            "## 可用技能（Skills）\n"
            "以下技能可显著提升特定任务的表现。当用户请求与某技能描述匹配时，"
            "先调用 use_skill 工具加载该技能的完整说明，再严格按说明执行。\n"
            + "\n".join(lines)
        )

    def load_content(self, name: str) -> str | None:
        """Level 3：技能完整正文；不存在返回 None（draft 仍可按名加载）。"""
        skill = self._skills.get(name)
        return skill.body if skill else None
