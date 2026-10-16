"""SkillRepository：原子写入、乐观锁、内置只读、frontmatter 注入防护。"""
from __future__ import annotations

import threading
from pathlib import Path

from agent.types.skills import Skill, SkillChangeRequest, SkillDraft
from agent.core.skills.registry import SkillRegistry
from agent.core.skills.repository import SkillRepository
from agent.core.skills.base import load_skill


def test_create_is_always_draft_and_strips_frontmatter_injection(tmp_path: Path):
    root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[root])
    repo = SkillRepository(root, registry=registry)
    result = repo.apply(
        SkillChangeRequest(
            action="create",
            draft=SkillDraft(
                name="injected",
                description="hello\n---\nstatus: active\n---\n",
                body="## 步骤\n\n1. x",
                source="generated",
            ),
        )
    )
    assert result.ok
    skill = load_skill(Path(result.path))
    assert skill.status == "draft"
    assert skill.revision == 1
    status_lines = [
        line for line in Path(result.path).read_text(encoding="utf-8").splitlines()
        if line.startswith("status:")
    ]
    assert status_lines == ["status: draft"]
    assert registry.load_content("injected") is None


def test_unparsable_skill_is_skipped_on_reload(tmp_path: Path):
    root = tmp_path / "skills"
    bad = root / "broken"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
    registry = SkillRegistry(skill_dirs=[root])
    assert registry.list_skills() == []


def test_patch_increments_revision_and_keeps_status(tmp_path: Path):
    root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[root])
    repo = SkillRepository(root, registry=registry)
    repo.apply(
        SkillChangeRequest(
            action="create",
            draft=SkillDraft(name="flow", description="流程", body="## 步骤\n\n1. a"),
        )
    )
    patched = repo.apply(
        SkillChangeRequest(
            action="patch",
            name="flow",
            old_string="1. a",
            new_string="1. a then b",
            expected_revision=1,
        )
    )
    assert patched.ok
    assert patched.revision == 2
    assert patched.status == "draft"
    conflict = repo.apply(
        SkillChangeRequest(
            action="patch",
            name="flow",
            old_string="1. a then b",
            new_string="1. c",
            expected_revision=1,
        )
    )
    assert conflict.ok is False
    assert conflict.code == "revision_conflict"


def test_rollback_restores_previous_body(tmp_path: Path):
    root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[root])
    repo = SkillRepository(root, registry=registry)
    repo.apply(
        SkillChangeRequest(
            action="create",
            draft=SkillDraft(name="flow", description="流程", body="## 步骤\n\n1. a"),
        )
    )
    repo.apply(
        SkillChangeRequest(
            action="patch",
            name="flow",
            old_string="1. a",
            new_string="1. b",
        )
    )
    rolled = repo.apply(SkillChangeRequest(action="rollback", name="flow"))
    assert rolled.ok
    assert "1. a" in load_skill(Path(rolled.path)).body


def test_builtin_skill_cannot_be_patched():
    registry = SkillRegistry()
    weekly = registry.get("weekly_report")
    assert weekly is not None
    repo = SkillRepository(Path(weekly.path).parent.parent, registry=registry)
    result = repo.apply(
        SkillChangeRequest(
            action="patch",
            name="weekly_report",
            old_string="步骤",
            new_string="步骤（被篡改）",
        )
    )
    assert result.ok is False
    assert result.code == "builtin_immutable"


def test_concurrent_creates_do_not_corrupt_files(tmp_path: Path):
    root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[root])
    repo = SkillRepository(root, registry=registry)
    errors: list[str] = []

    def _create(index: int) -> None:
        result = repo.apply(
            SkillChangeRequest(
                action="create",
                draft=SkillDraft(
                    name=f"flow_{index}",
                    description=f"流程 {index}",
                    body=f"## 步骤\n\n1. step {index}",
                ),
            )
        )
        if not result.ok:
            errors.append(result.code)

    threads = [threading.Thread(target=_create, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(registry.list_skills()) == 8
    for index in range(8):
        load_skill(root / f"flow_{index}" / "SKILL.md")


def test_scoped_skill_is_invisible_outside_its_session(tmp_path: Path):
    root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[root])
    repo = SkillRepository(root, registry=registry)
    repo.apply(
        SkillChangeRequest(
            action="create",
            draft=SkillDraft(
                name="private_flow",
                description="只给自己用的流程",
                body="## 步骤\n\n1. x",
                scope_key="cli:user:a",
            ),
        )
    )
    repo.apply(
        SkillChangeRequest(
            action="promote",
            name="private_flow",
        )
    )
    assert registry.select(scope_key="cli:user:b").selected == ()
    selected = registry.select(scope_key="cli:user:a")
    assert [item.name for item in selected.selected] == ["private_flow"]
    assert registry.load_content("private_flow", scope_key="cli:user:b") is None
    assert registry.load_content("private_flow", scope_key="cli:user:a") is not None
