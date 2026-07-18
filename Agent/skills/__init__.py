"""
Agent/skills 包 — 技能系统（L3 程序性记忆的最小实现）

技能 = 一份带元信息的 Markdown 说明书（SKILL.md），教会 Agent 完成某类任务：
    - Level 1：name + 一句话描述，始终注入 system prompt（省 token）
    - Level 3：完整正文，LLM 调用 use_skill 工具时才加载

目录约定：
    agent/skills/builtin/<skill_name>/SKILL.md   # 内置技能
"""
from .base import Skill
from .registry import SkillRegistry
from .skill_tool import UseSkillTool

__all__ = ["Skill", "SkillRegistry", "UseSkillTool"]
