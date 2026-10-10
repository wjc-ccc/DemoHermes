"""
UseSkillTool — 技能调用工具（Skill 与 Tool 体系的桥梁）

LLM 看到 system prompt 里的 Level 1 技能摘要后，
通过调用本工具加载某个技能的完整说明（作为工具结果回注），
随后按说明执行任务。这样「技能调用」复用了「工具调用」的全套链路，
Loop 无需为 Skill 单开分支。

可加载范围严格等于本 turn 冻结的 SkillSelection：
    - 模型猜到 draft 名称也加载不到，绕不过质量门
    - 同 turn 内发生的写盘/晋升不改变本 turn 已冻结的正文
ToolInvocationContext 传入本轮冻结的技能快照。
"""
from __future__ import annotations

from agent.tools.base import Tool
from agent.types.action.policy import ToolPolicy
from agent.types.tools import ToolCallResult, ToolInvocationContext
from agent.skills.registry import SkillRegistry
from agent.types.skills import SkillSelection


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
    action_policy = ToolPolicy(when_to_use=description, when_not_to_use="不用于产生业务写入")

    def __init__(self, registry: SkillRegistry):
        self._registry = registry

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        skill_name = str(arguments.get("skill_name", "")).strip()
        if not skill_name:
            return ToolCallResult(ok=False, error="缺少参数 skill_name")

        selection = context.skills
        if isinstance(selection, SkillSelection):
            ref = selection.load(skill_name)
            if ref is None:
                available = [item.name for item in selection.selected]
                return ToolCallResult(
                    ok=False,
                    error=f"技能不可用: {skill_name}（本轮可用: {available}）",
                )
            return self._loaded(ref.name, ref.body, revision=ref.revision)

        content = self._registry.load_content(skill_name)
        if content is None:
            return ToolCallResult(
                ok=False,
                error=f"技能不可用: {skill_name}（未启用或不存在）",
            )
        skill = self._registry.get(skill_name)
        return self._loaded(
            skill_name, content, revision=skill.revision if skill else 1
        )

    @staticmethod
    def _loaded(name: str, body: str, *, revision: int) -> ToolCallResult:
        return ToolCallResult(
            ok=True,
            result_text=f"已加载技能「{name}」，请严格按以下说明执行：\n\n{body}",
            data={"skill_name": name, "skill_revision": revision},
        )
