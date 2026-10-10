"""
SkillRegistry — 技能注册表（只读面）

启动时扫描技能目录：
    agent/skills/builtin/*/SKILL.md   （内置，人工维护）
    data/skills/*/SKILL.md            （运行期沉淀，目录存在才扫描）

对 Loop 提供的核心接口是「一次选择、整轮冻结」：
    select(...)        : 返回 SkillSelection —— L1 摘要 + 本轮可加载的正文快照
    summaries_prompt() : select() 的薄封装，只要 L1 文本时用
    load_content(name) : 仅 active 且作用域可见的技能才返回正文

三条硬规则：
    1. 只有 active 技能进入 L1 摘要与 use_skill 可加载范围；draft 不可被发现也不可被猜到
    2. 带 scope 的生成型技能只对该会话可见，不跨用户泄漏
    3. 重名不静默覆盖：先扫描到的（内置优先）保留，冲突记录 error 并跳过

写入一律走 SkillRepository，本模块不改文件。
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path


from agent.types.skills import Skill, SkillRef, SkillSelection
from agent.skills.base import SkillParseError
from ..provider import ROOT

from agent.skills.base import load_skill

logger = logging.getLogger(__name__)

# 默认扫描目录：内置技能 + 运行期自建技能
DEFAULT_SKILL_DIRS = [
    Path(__file__).resolve().parent / "builtin",
    ROOT / "data" / "skills",
]

_SUMMARY_HEADER = (
    "## 可用技能（Skills）\n"
    "以下技能可显著提升特定任务的表现。当用户请求与某技能描述匹配时，"
    "先调用 use_skill 工具加载该技能的完整说明，再严格按说明执行。\n"
)


class SkillRegistry:
    def __init__(self, skill_dirs: list[Path] | None = None, *, selector=None) -> None:
        self.selector = selector
        self._skill_dirs = list(
            skill_dirs if skill_dirs is not None else DEFAULT_SKILL_DIRS
        )
        self._skills: dict[str, Skill] = {}
        self.reload()

    # ---- 扫描注册 ----
    def reload(self) -> None:
        """重新扫描全部 skill_dirs（Repository 写盘后热更新）。

        先构建完整的新映射再整体替换，避免并发读取时看到空 Registry。
        """
        scanned: dict[str, Skill] = {}
        for directory in self._skill_dirs:
            self._scan_dir(directory, scanned)
        self._skills = scanned

    def _scan_dir(self, directory: Path, scanned: dict[str, Skill]) -> None:
        if not directory.exists():
            return
        for skill_file in sorted(directory.glob("*/SKILL.md")):
            try:
                skill = load_skill(skill_file)
            except SkillParseError:
                # fail closed：无法确认的技能不进上下文
                logger.exception("技能文件解析失败，已跳过: %s", skill_file)
                continue
            kept = scanned.get(skill.name)
            if kept is not None:
                logger.error(
                    "技能重名冲突，保留先扫描到的: name=%s kept=%s skipped=%s",
                    skill.name, kept.path, skill.path,
                )
                continue
            scanned[skill.name] = skill

    def register(self, skill: Skill) -> None:
        """显式注册（测试与内存装配用）；扫描路径不走这里。"""
        self._skills[skill.name] = skill
        logger.info(
            "注册技能 name=%s status=%s rev=%s path=%s",
            skill.name, skill.status, skill.revision, skill.path,
        )

    # ---- 查询 ----
    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_skills(self) -> list[str]:
        return list(self._skills)

    def active_skills(
        self,
        *,
        scope_key: str | None = None,
        available_tools: Sequence[str] | None = None,
    ) -> list[Skill]:
        """可用于本次装配的 active 技能：状态 + 作用域 + 依赖工具三重过滤。"""
        tools = set(available_tools) if available_tools is not None else None
        result: list[Skill] = []
        for skill in self._skills.values():
            if not skill.is_active:
                continue
            if not skill.visible_in(scope_key):
                continue
            if tools is not None and not set(skill.required_tools).issubset(tools):
                logger.debug(
                    "技能依赖工具缺失，跳过 name=%s 需要=%s",
                    skill.name, skill.required_tools,
                )
                continue
            result.append(skill)
        return result

    # ---- 选择（本 turn 冻结）----
    def select(
        self,
        *,
        query: str | None = None,
        selector=None,
        top_k: int = 5,
        scope_key: str | None = None,
        available_tools: Sequence[str] | None = None,
    ) -> SkillSelection:
        """选出本 turn 可用技能并冻结其正文。

        有 query + selector 时只注入相关子集（渐进披露）；无匹配则返回空选择，
        宁缺毋滥，避免用无关技能灌满上下文。
        """
        selector = selector if selector is not None else self.selector
        candidates = self.active_skills(
            scope_key=scope_key, available_tools=available_tools
        )
        if not candidates:
            return SkillSelection()
        if query and selector is not None:
            candidates = selector.select(query, candidates, top_k=top_k)
        else:
            candidates = sorted(candidates, key=lambda s: s.name)
        if not candidates:
            return SkillSelection()
        return SkillSelection(
            summary_prompt=_SUMMARY_HEADER
            + "\n".join(skill.summary_line() for skill in candidates),
            selected=tuple(
                SkillRef(
                    name=skill.name,
                    revision=skill.revision,
                    content_hash=skill.content_hash,
                    description=skill.description,
                    body=skill.body,
                )
                for skill in candidates
            ),
        )

    def summaries_prompt(
        self,
        *,
        query: str | None = None,
        selector=None,
        top_k: int = 5,
        scope_key: str | None = None,
        available_tools: Sequence[str] | None = None,
    ) -> str:
        """Level 1 摘要文本；等价于 select(...).summary_prompt。"""
        return self.select(
            query=query,
            selector=selector,
            top_k=top_k,
            scope_key=scope_key,
            available_tools=available_tools,
        ).summary_prompt

    def load_content(self, name: str, *, scope_key: str | None = None) -> str | None:
        """Level 2：完整正文。draft/disabled 与跨作用域技能一律不可加载。"""
        skill = self._skills.get((name or "").strip())
        if skill is None or not skill.is_active:
            return None
        if not skill.visible_in(scope_key):
            return None
        return skill.body
