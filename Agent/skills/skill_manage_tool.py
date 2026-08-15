"""SkillManageTool — Hermes 式 skill_manage：会话内 create / patch Skill。

写盘到 data/skills/，并热 reload Registry；patch 保留 .bak.n 备份。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..tools.base import Tool, ToolCallResult
from .registry import SkillRegistry


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "").strip()).strip("_")


def _next_bak(path: Path) -> Path:
    n = 1
    while True:
        bak = path.with_name(f"SKILL.md.bak.{n}")
        if not bak.exists():
            return bak
        n += 1


class SkillManageTool(Tool):
    name = "skill_manage"
    description = (
        "管理程序性技能（Skills）：create 新建 SKILL.md，patch 用 old_string/new_string "
        "小步修正。复杂多步任务成功后可沉淀为技能；使用技能发现问题应立刻 patch。"
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
                "description": "一句话描述（create 时必填）",
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

    def __init__(self, registry: SkillRegistry, *, skills_root: Path) -> None:
        self._registry = registry
        self._root = Path(skills_root)

    def execute(self, arguments: dict) -> ToolCallResult:
        action = str(arguments.get("action", "")).strip()
        name = _safe_name(str(arguments.get("name", "")))
        if not name:
            return ToolCallResult(ok=False, error="缺少有效 name")
        if action == "create":
            return self._create(name, arguments)
        if action == "patch":
            return self._patch(name, arguments)
        return ToolCallResult(ok=False, error=f"不支持的 action: {action}")

    def _create(self, name: str, arguments: dict) -> ToolCallResult:
        description = str(arguments.get("description", "")).strip()
        body = str(arguments.get("body", "")).strip()
        if not description or not body:
            return ToolCallResult(ok=False, error="create 需要 description 与 body")
        skill_dir = self._root / name
        path = skill_dir / "SKILL.md"
        if path.exists():
            return ToolCallResult(ok=False, error=f"技能已存在: {name}")
        if not body.startswith("#"):
            body = f"# {name}\n\n{body}"
        text = (
            f"---\nname: {name}\ndescription: {description}\nstatus: draft\n---\n\n"
            f"{body}\n"
        )
        skill_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._registry.reload()
        return ToolCallResult(
            ok=True,
            result_text=f"已创建 draft 技能「{name}」",
            data={"skill_name": name, "path": str(path)},
        )

    def _patch(self, name: str, arguments: dict) -> ToolCallResult:
        old = str(arguments.get("old_string", ""))
        new = str(arguments.get("new_string", ""))
        if not old:
            return ToolCallResult(ok=False, error="patch 需要 old_string")
        skill = self._registry.get(name)
        if skill is None or not skill.path:
            return ToolCallResult(ok=False, error=f"技能不存在: {name}")
        path = Path(skill.path)
        if not path.is_file():
            return ToolCallResult(ok=False, error=f"技能文件缺失: {name}")
        before = path.read_text(encoding="utf-8")
        if old not in before:
            return ToolCallResult(ok=False, error="old_string 未在 SKILL.md 中找到")
        bak = _next_bak(path)
        bak.write_text(before, encoding="utf-8")
        after = before.replace(old, new, 1)
        path.write_text(after, encoding="utf-8")
        self._registry.reload()
        return ToolCallResult(
            ok=True,
            result_text=f"已 patch 技能「{name}」（备份 {bak.name}）",
            data={"skill_name": name, "bak": str(bak)},
        )
