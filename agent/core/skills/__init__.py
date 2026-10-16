"""
agent/core/skills 包 — 技能系统（程序性资产）

技能 = 版本化、可评测、按条件选择的 Markdown 程序性知识：
    - Level 1：name + 描述（做什么 + 何时使用），只有 active 技能进 system prompt
    - Level 2：完整正文，LLM 调用 use_skill 时才加载，且限定为本 turn 冻结的版本

两个面：
    SkillRegistry   只读：扫描、选择（select）、加载
    SkillRepository 只写：create / patch / promote / disable / rollback

目录约定：
    agent/core/skills/builtin/<skill_name>/SKILL.md   # 内置，人工维护
    data/skills/<skill_name>/SKILL.md            # 运行期沉淀的候选
"""
from agent.core.skills.base import SkillParseError, normalize_skill_name
from agent.core.skills.registry import SkillRegistry
from agent.core.skills.repository import SkillRepository
from .skill_manage_tool import SkillManageTool
from .skill_tool import UseSkillTool

__all__ = ['SkillManageTool', 'SkillParseError', 'SkillRegistry', 'SkillRepository', 'UseSkillTool', 'normalize_skill_name']
