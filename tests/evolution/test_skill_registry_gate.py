"""Phase 3.2 / 3.4：Registry.reload + draft/active Gate。

公共 seam：
  - SkillRegistry.reload() 重新扫描目录
  - summaries_prompt() 只包含 status=active（缺省视为 active，兼容 builtin）
  - SkillCreator 写出的 draft 不出现在 L1，直到 promote
"""
from __future__ import annotations

from pathlib import Path

from Agent.evolution.skill_creator import SkillCreator
from Agent.evolution.types import EvolutionSignal
from Agent.memory.types import MemoryScope, TurnEnvelope
from Agent.skills import SkillRegistry
from Agent.skills.base import Skill

_SCOPE = MemoryScope(kind="personal", key="cli:user:skill2")


def _turn(turn_id: str) -> TurnEnvelope:
    return TurnEnvelope(
        turn_id=turn_id,
        session_id="s1",
        scope=_SCOPE,
        user_message_id="m_user",
        assistant_message_ids=(),
        tool_call_ids=("c1", "c2"),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def _signals(turn_id: str) -> EvolutionSignal:
    return EvolutionSignal(
        turn_id=turn_id,
        completed=True,
        tool_sequence=("calculator", "memory_read"),
    )


def test_registry_reload_picks_up_new_skill_file(tmp_path: Path):
    """写盘后 reload，list_skills 可见新 skill。"""
    root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[root])
    assert registry.list_skills() == []

    creator = SkillCreator(skills_root=root, frequency_threshold=1, registry=registry)
    path = creator.consider(_turn("t1"), _signals("t1"))
    assert path is not None
    assert "calculator_memory_read" in registry.list_skills()


def test_draft_skill_excluded_from_l1_summaries(tmp_path: Path):
    """draft 不进 L1；active 才进 summaries_prompt。"""
    root = tmp_path / "skills"
    draft_dir = root / "draft_flow"
    draft_dir.mkdir(parents=True)
    (draft_dir / "SKILL.md").write_text(
        "---\nname: draft_flow\ndescription: 草稿流程\nstatus: draft\n---\n\n# body\n",
        encoding="utf-8",
    )
    active_dir = root / "active_flow"
    active_dir.mkdir(parents=True)
    (active_dir / "SKILL.md").write_text(
        "---\nname: active_flow\ndescription: 已激活流程\nstatus: active\n---\n\n# body\n",
        encoding="utf-8",
    )

    registry = SkillRegistry(skill_dirs=[root])
    prompt = registry.summaries_prompt()
    assert "active_flow" in prompt
    assert "draft_flow" not in prompt
    assert registry.get("draft_flow") is not None  # 仍可 load_content


def test_promote_draft_then_appears_in_l1(tmp_path: Path):
    """Gate promote 后 status=active，L1 出现。"""
    from Agent.evolution.skill_gate import SkillLifecycleGate

    root = tmp_path / "skills"
    skill_dir = root / "promo_flow"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\nname: promo_flow\ndescription: 待晋升\nstatus: draft\n---\n\n# body\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(skill_dirs=[root])
    assert "promo_flow" not in registry.summaries_prompt()

    gate = SkillLifecycleGate(registry)
    assert gate.record_success("promo_flow") is False  # 未达阈值
    assert gate.record_success("promo_flow") is True   # 默认 M=2
    assert Skill.from_file(skill_path).status == "active"
    registry.reload()
    assert "promo_flow" in registry.summaries_prompt()
