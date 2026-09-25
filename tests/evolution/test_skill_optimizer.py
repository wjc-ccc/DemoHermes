"""Phase 3.3：SkillOptimizer — 按「技能生效期间的失败」提出正文补丁。

公共 seam：
  - SkillOptimizer.propose(turn, session) → SkillPatchProposal
  - draft 可由 SkillRepository.patch 立即应用；active 要过 replay
"""
from __future__ import annotations

from pathlib import Path

from agent.core.types import Session, ToolCall
from agent.evolution.skill_optimizer import SkillOptimizer
from agent.evolution.types import EvolutionSignal
from agent.core.memory import MemoryScope
from agent.core.memory.types import TurnRecord
from agent.skills import SkillRegistry
from agent.skills.repository import SkillChangeRequest, SkillRepository


_SCOPE = MemoryScope(tenant_id="cli", user_id="opt1")


def _turn(turn_id: str = "t1") -> TurnRecord:
    return TurnRecord(
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


def _write_skill(root: Path, name: str = "broken_flow", *, status: str = "draft") -> Path:
    d = root / name
    d.mkdir(parents=True)
    path = d / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: 易失败流程\nstatus: {status}\n---\n\n"
        f"# {name}\n\n## 步骤\n\n1. 做一件事\n\n## 失败处理\n\n- 无\n",
        encoding="utf-8",
    )
    return path


def _session_with_skill_then_failure(turn_id: str = "t9") -> Session:
    return Session(
        session_id="s1",
        tool_calls=[
            ToolCall(
                tool_call_id="c1",
                trajectory_id=turn_id,
                tool_name="use_skill",
                arguments={"skill_name": "broken_flow"},
                status="success",
                tool_call_result={"skill_name": "broken_flow", "skill_revision": 1},
                created_at=1.0,
            ),
            ToolCall(
                tool_call_id="c2",
                trajectory_id=turn_id,
                tool_name="calculator",
                status="error",
                error="division by zero",
                created_at=2.0,
            ),
        ],
    )


def test_optimizer_proposes_patch_for_attributed_failure(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root)
    registry = SkillRegistry(skill_dirs=[root])
    optimizer = SkillOptimizer(registry)
    proposals = optimizer.propose(_turn("t9"), _session_with_skill_then_failure())
    assert len(proposals) == 1
    assert proposals[0].skill_name == "broken_flow"
    assert "division by zero" in proposals[0].new_string


def test_optimizer_draft_patch_applies_via_repository(tmp_path: Path):
    root = tmp_path / "skills"
    skill_path = _write_skill(root, status="draft")
    before = skill_path.read_text(encoding="utf-8")
    registry = SkillRegistry(skill_dirs=[root])
    repo = SkillRepository(root, registry=registry)
    optimizer = SkillOptimizer(registry)
    proposal = optimizer.propose(_turn("t9"), _session_with_skill_then_failure())[0]
    applied = repo.apply(
        SkillChangeRequest(
            action="patch",
            name=proposal.skill_name,
            old_string=proposal.old_string,
            new_string=proposal.new_string,
            actor="test",
        )
    )
    assert applied.ok
    after = skill_path.read_text(encoding="utf-8")
    assert after != before
    assert "division by zero" in after
    assert (skill_path.parent / "SKILL.md.bak.1").is_file()


def test_optimizer_noop_without_skill_or_failure(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root)
    registry = SkillRegistry(skill_dirs=[root])
    optimizer = SkillOptimizer(registry)
    empty = Session(session_id="s1")
    assert optimizer.propose(_turn("t1"), empty) == ()
    assert (
        optimizer.propose(
            _turn("t2"),
            Session(
                session_id="s1",
                tool_calls=[
                    ToolCall(
                        tool_call_id="c1",
                        trajectory_id="t2",
                        tool_name="use_skill",
                        arguments={"skill_name": "broken_flow"},
                        status="success",
                        tool_call_result={"skill_name": "broken_flow"},
                    )
                ],
            ),
            EvolutionSignal(turn_id="t2", skills_used=("broken_flow",)),
        )
        == ()
    )
