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
from ..provider import ROOT

logger = logging.getLogger(__name__)

# 默认扫描目录：内置技能 + 运行期自建技能
DEFAULT_SKILL_DIRS = [
    Path(__file__).resolve().parent / "builtin",
    ROOT / "data" / "skills",
]


class SkillRegistry:
    def __init__(self, skill_dirs: list[Path] | None = None) -> None:
        self._skills: dict[str, Skill] = {}
        for directory in skill_dirs if skill_dirs is not None else DEFAULT_SKILL_DIRS:
            self._scan_dir(directory)

    # ---- 扫描注册 ----
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
        logger.info("注册技能 name=%s path=%s", skill.name, skill.path)

    # ---- 查询 ----
    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_skills(self) -> list[str]:
        return list(self._skills)

    # ---- 两级加载 ----
    def summaries_prompt(self) -> str:
        """Level 1：所有技能的一行摘要，拼成 prompt 块；无技能时返回空串。"""
        if not self._skills:
            return ""
        lines = [skill.summary_line() for skill in self._skills.values()]
        return (
            "## 可用技能（Skills）\n"
            "以下技能可显著提升特定任务的表现。当用户请求与某技能描述匹配时，"
            "先调用 use_skill 工具加载该技能的完整说明，再严格按说明执行。\n"
            + "\n".join(lines)
        )

    def load_content(self, name: str) -> str | None:
        """Level 3：技能完整正文；不存在返回 None。"""
        skill = self._skills.get(name)
        return skill.body if skill else None
