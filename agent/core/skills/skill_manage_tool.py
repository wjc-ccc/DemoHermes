"""SkillManageTool — 会话内 create / patch 技能候选。

一切写入都委托 SkillRepository，因此模型无法：
    - 直接把 status 写成 active（create 恒为 draft，patch 只作用于正文）
    - 修改内置技能
    - 覆盖同名技能或重复沉淀同样的正文
新建技能保持 draft，由可信管理入口审查后启用。
"""
from __future__ import annotations

from agent.core.tools.base import Tool
from agent.types.action.policy import ToolPolicy
from agent.types.tools import ToolCallResult, ToolInvocationContext
from agent.types.skills import SkillChangeRequest, SkillDraft
from agent.core.skills.repository import SkillRepository

_ERROR_HINTS = {
    "exists": "同名技能已存在，请改用 patch",
    "duplicate_content": "已有内容相同的技能，无需重复沉淀",
    "builtin_immutable": "内置技能不可修改",
    "name_conflict": "与内置技能同名，请换名字",
    "not_found": "技能不存在",
    "not_matched": "old_string 未在正文中找到",
    "no_change": "patch 没有改变正文",
    "revision_conflict": "技能已被其他改动更新，请重新读取后再试",
}


class SkillManageTool(Tool):
    name = "skill_manage"
    description = (
        "管理程序性技能（Skills）：create 新建技能候选，patch 用 old_string/new_string "
        "小步修正正文。复杂多步任务成功后可沉淀为候选；使用技能发现问题应立刻 patch。"
        "新建技能保持草案，需人工审查后启用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "patch"],
                "description": "create 或 patch",
            },
            "name": {"type": "string", "description": "技能名（slug）"},
            "description": {
                "type": "string",
                "description": "一句话描述：做什么 + 何时使用（create 时必填）",
            },
            "body": {
                "type": "string",
                "description": "SKILL 正文 Markdown（create 时必填；含步骤/失败处理）",
            },
            "old_string": {"type": "string", "description": "patch：要替换的原文"},
            "new_string": {"type": "string", "description": "patch：替换后的文本"},
            # ACTION-HOOK-07：action PRE 检查读版本，Repository 在写入处再次比较。
            # 关闭 action 时仍可显式使用这一业务前置条件。
            "expected_revision": {"type": "integer", "minimum": 1},
        },
        "required": ["action", "name"],
    }
    action_policy = ToolPolicy(when_to_use="创建或修正技能候选",
        when_not_to_use="不用于修改内置技能或自动晋升",
        effect="write", risk="medium", permission="user", idempotency="keyed",
        key_fields=("action", "name", "expected_revision"))
    action_operations = {
        "patch": action_policy.model_copy(update={"precondition_field": "expected_revision"}),
    }

    def __init__(self, repository: SkillRepository) -> None:
        self._repo = repository

    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        action = str(arguments.get("action", "")).strip()
        name = str(arguments.get("name", "")).strip()
        if action == "create":
            request = SkillChangeRequest(
                action="create",
                name=name,
                draft=SkillDraft(
                    name=name,
                    description=str(arguments.get("description", "")).strip(),
                    body=str(arguments.get("body", "")).strip(),
                    source="user",
                ),
                actor="model",
                reason="skill_manage create",
            )
        elif action == "patch":
            request = SkillChangeRequest(
                action="patch",
                name=name,
                old_string=str(arguments.get("old_string", "")),
                new_string=str(arguments.get("new_string", "")),
                expected_revision=arguments.get("expected_revision"),
                actor="model",
                reason="skill_manage patch",
            )
        else:
            return ToolCallResult(ok=False, error=f"不支持的 action: {action}")

        result = self._repo.apply(request)
        if not result.ok:
            hint = _ERROR_HINTS.get(result.code, result.detail or result.code)
            return ToolCallResult(ok=False, error=f"{hint}（{result.code}）")
        if result.code == "created":
            text = f"已创建技能候选「{result.name}」，需人工审查后才会启用"
        else:
            text = f"已修正技能「{result.name}」正文（revision {result.revision}）"
        return ToolCallResult(
            ok=True,
            result_text=text,
            data={
                "skill_name": result.name,
                "revision": result.revision,
                "status": result.status,
                "path": result.path,
            },
        )
