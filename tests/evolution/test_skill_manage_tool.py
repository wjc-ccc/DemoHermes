"""Hermes 式 skill_manage 工具：Agent 可在会话中 create/patch Skill。

公共 seam：SkillManageTool.execute({action, ...}) → ToolCallResult
"""
from __future__ import annotations

from pathlib import Path

from Agent.skills import SkillRegistry
from Agent.skills.skill_manage_tool import SkillManageTool


def test_skill_manage_create_and_patch(tmp_path: Path):
    root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[root])
    tool = SkillManageTool(registry, skills_root=root)

    created = tool.execute({
        "action": "create",
        "name": "staging_deploy",
        "description": "部署到 staging",
        "body": "## 步骤\n\n1. 构建\n2. 推送\n",
    })
    assert created.ok
    assert "staging_deploy" in registry.list_skills()
    path = root / "staging_deploy" / "SKILL.md"
    assert path.is_file()

    patched = tool.execute({
        "action": "patch",
        "name": "staging_deploy",
        "old_string": "2. 推送",
        "new_string": "2. 推送并健康检查",
    })
    assert patched.ok
    text = path.read_text(encoding="utf-8")
    assert "健康检查" in text
    bak = list(path.parent.glob("SKILL.md.bak.*"))
    assert bak
