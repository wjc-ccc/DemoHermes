"""删除自动演化后，普通技能仍按状态和可信管理变化可见性。"""
from agent.skills.registry import SkillRegistry
from agent.skills.repository import SkillRepository
from agent.types.skills import SkillChangeRequest, SkillDraft


def test_created_draft_stays_hidden_until_trusted_management_promotes_it(tmp_path):
    root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[root])
    repository = SkillRepository(root, registry=registry)
    created = repository.apply(SkillChangeRequest(
        action="create", name="draft_flow", actor="user",
        draft=SkillDraft(name="draft_flow", description="草稿流程", body="# body", source="user"),
    ))
    assert created.ok and created.status == "draft"
    assert "draft_flow" in registry.list_skills()
    assert "draft_flow" not in registry.summaries_prompt()
    assert registry.load_content("draft_flow") is None
    promoted = repository.apply(SkillChangeRequest(action="promote", name="draft_flow", actor="user"))
    assert promoted.ok
    assert "draft_flow" in registry.summaries_prompt()
    assert registry.load_content("draft_flow") == "# body"
