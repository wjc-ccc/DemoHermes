"""Phase 3.3：SkillOptimizer — use_skill 后失败 → 小 diff patch + 版本备份。

公共 seam：
  - SkillOptimizer.optimize(turn, signals) → 有 skills_used + tool_failures 时 patch
  - 旧版备份为 SKILL.md.bak.<n>；正文追加失败处理条目
观测：读 SKILL.md 与.bak 文件。
"""
from __future__ import annotations

from pathlib import Path

from Agent.evolution.types import EvolutionSignal
from Agent.memory.types import MemoryScope, TurnRecord
from Agent.skills import SkillRegistry

_SCOPE = MemoryScope(kind="personal", key="cli:user:opt1")


def _turn(turn_id: str = "t1") -> TurnRecord:
    return TurnRecord(
        turn_id=turn_id,
        session_id="s1",
        scope=_SCOPE,
        user_message_id="m_user",
        assistant_message_ids=(),
        tool_call_ids=("c1",),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def _write_skill(root: Path, name: str = "broken_flow") -> Path:
    d = root / name
    d.mkdir(parents=True)
    path = d / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: 易失败流程\nstatus: active\n---\n\n"
        f"# {name}\n\n## 步骤\n\n1. 做一件事\n\n## 失败处理\n\n- 无\n",
        encoding="utf-8",
    )
    return path


def test_optimizer_patches_skill_and_keeps_backup(tmp_path: Path):
    """use_skill 后出现 tool_failures → patch 正文，并留下 .bak。"""
    from Agent.evolution.skill_optimizer import SkillOptimizer

    root = tmp_path / "skills"
    skill_path = _write_skill(root)
    before = skill_path.read_text(encoding="utf-8")
    registry = SkillRegistry(skill_dirs=[root])
    optimizer = SkillOptimizer(registry)

    patched = optimizer.optimize(
        _turn("t9"),
        EvolutionSignal(
            turn_id="t9",
            skills_used=("broken_flow",),
            tool_failures=("calculator: division by zero",),
            tool_sequence=("use_skill", "calculator"),
        ),
    )

    assert patched is True
    after = skill_path.read_text(encoding="utf-8")
    assert after != before
    assert "division by zero" in after
    bak = skill_path.with_name("SKILL.md.bak.1")
    assert bak.is_file()
    assert bak.read_text(encoding="utf-8") == before


def test_optimizer_noop_without_skill_or_failure(tmp_path: Path):
    """无 skills_used 或无失败时不改文件。"""
    from Agent.evolution.skill_optimizer import SkillOptimizer

    root = tmp_path / "skills"
    skill_path = _write_skill(root)
    before = skill_path.read_text(encoding="utf-8")
    registry = SkillRegistry(skill_dirs=[root])
    optimizer = SkillOptimizer(registry)

    assert optimizer.optimize(_turn("t1"), None) is False
    assert (
        optimizer.optimize(
            _turn("t2"),
            EvolutionSignal(turn_id="t2", skills_used=("broken_flow",)),
        )
        is False
    )
    assert skill_path.read_text(encoding="utf-8") == before
