"""Phase 3.1：SkillCreator — 可复用多步流程 → data/skills/<slug>/SKILL.md。

公共 seam：
  - SkillCreator.consider(turn, signals) → 达频率阈值则写盘并返回 path
  - 产物为 AWM recipe 风格 SKILL.md（frontmatter + 目标/步骤）
  - 默认 status=draft；不经 memory/procedural 第二套存储
观测：读文件系统 + SkillRegistry.reload/list；不 mock Registry 内部。
"""
from __future__ import annotations

from pathlib import Path

from Agent.memory.types import MemoryScope, TurnRecord
from Agent.evolution.types import EvolutionSignal

_SCOPE = MemoryScope(kind="personal", key="cli:user:skill1")


def _turn(turn_id: str = "t1", *, completed: bool = True) -> TurnRecord:
    return TurnRecord(
        turn_id=turn_id,
        session_id="s1",
        scope=_SCOPE,
        user_message_id="m_user",
        assistant_message_ids=(),
        tool_call_ids=("c1", "c2", "c3"),
        started_at=1000.0,
        finished_at=1001.0,
        completed=completed,
        interrupted=False,
        error=None,
    )


def _signals(
    turn_id: str,
    *,
    tools: tuple[str, ...] = ("calculator", "memory_read"),
    failures: tuple[str, ...] = (),
) -> EvolutionSignal:
    return EvolutionSignal(
        turn_id=turn_id,
        completed=True,
        tool_failures=failures,
        tool_sequence=tools,
    )


def test_creator_writes_skill_after_frequency_threshold(tmp_path: Path):
    """同一工具序列成功出现达到阈值后，写出 draft SKILL.md。"""
    from Agent.evolution.skill_creator import SkillCreator

    root = tmp_path / "skills"
    creator = SkillCreator(skills_root=root, frequency_threshold=2)

    assert creator.consider(_turn("t1"), _signals("t1")) is None
    path = creator.consider(_turn("t2"), _signals("t2"))

    assert path is not None
    assert path.name == "SKILL.md"
    assert path.parent.name == "calculator_memory_read"
    text = path.read_text(encoding="utf-8")
    assert "name: calculator_memory_read" in text
    assert "status: draft" in text
    assert "## 目标" in text
    assert "## 步骤" in text
    assert "calculator" in text and "memory_read" in text


def test_creator_skips_when_failures_or_single_tool(tmp_path: Path):
    """有失败或单工具轨迹不创建 Skill。"""
    from Agent.evolution.skill_creator import SkillCreator

    creator = SkillCreator(skills_root=tmp_path / "skills", frequency_threshold=1)
    assert (
        creator.consider(
            _turn("t1"),
            _signals("t1", tools=("calculator", "memory_read"), failures=("calculator: boom",)),
        )
        is None
    )
    assert (
        creator.consider(_turn("t2"), _signals("t2", tools=("calculator",)))
        is None
    )


def test_creator_does_not_overwrite_existing(tmp_path: Path):
    """已存在同名 Skill 时不再写盘。"""
    from Agent.evolution.skill_creator import SkillCreator

    root = tmp_path / "skills"
    creator = SkillCreator(skills_root=root, frequency_threshold=1)
    first = creator.consider(_turn("t1"), _signals("t1"))
    assert first is not None
    second = creator.consider(_turn("t2"), _signals("t2"))
    assert second is None
