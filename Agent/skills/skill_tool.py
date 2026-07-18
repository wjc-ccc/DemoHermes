"""
UseSkillTool — 技能调用工具（Skill 与 Tool 体系的桥梁）

LLM 看到 system prompt 里的 Level 1 技能摘要后，
通过调用本工具加载某个技能的 Level 3 完整说明（作为工具结果回注），
随后按说明执行任务。这样「技能调用」复用了「工具调用」的全套链路，
Loop 无需为 Skill 单开分支。
"""
from __future__ import annotations

from ..tools.base import Tool, ToolResult
from .registry import SkillRegistry


class UseSkillTool(Tool):
    name = "use_skill"
    description = (
        "加载指定技能（Skill）的完整使用说明。当用户请求与 system prompt 中"
        "列出的某个技能匹配时，先调用本工具获取详细步骤，再按步骤执行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "技能名称，必须是 system prompt 中列出的技能名",
            }
        },
        "required": ["skill_name"],
    }

    def __init__(self, registry: SkillRegistry):
        self._registry = registry

    def execute(self, arguments: dict) -> ToolResult:
        skill_name = str(arguments.get("skill_name", "")).strip()
        if not skill_name:
            return ToolResult(ok=False, error="缺少参数 skill_name")
        content = self._registry.load_content(skill_name)
        if content is None:
            return ToolResult(
                ok=False,
                error=f"技能不存在: {skill_name}（可用: {self._registry.list_skills()}）",
            )
        return ToolResult(
            ok=True,
            result_text=f"已加载技能「{skill_name}」，请严格按以下说明执行：\n\n{content}",
            data={"skill_name": skill_name},
        )
