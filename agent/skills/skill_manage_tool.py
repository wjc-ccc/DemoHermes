"""SkillManageTool — 会话内 create / patch 技能候选。

一切写入都委托 SkillRepository，因此模型无法：
    - 直接把 status 写成 active（create 恒为 draft，patch 只作用于正文）
    - 修改内置技能
    - 覆盖同名技能或重复沉淀同样的正文
新建的技能是「候选」，能否上线由 Eval 回放质量门决定。
"""
from __future__ import annotations

from ..tools.base import Tool, ToolCallResult
from .repository import SkillChangeRequest, SkillDraft, SkillRepository

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
        "新建与修改都是候选，需通过评测回放才会正式启用。"
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
        },
        "required": ["action", "name"],
    }

    def __init__(self, repository: SkillRepository) -> None:
        self._repo = repository

    def execute(self, arguments: dict) -> ToolCallResult:
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
            text = f"已创建技能候选「{result.name}」，通过评测回放后才会启用"
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
