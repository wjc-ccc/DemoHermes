"""Hermes 式轨迹沉淀：复杂成功任务 → SkillProposal → SKILL.md。

公共 seam：
  - TrajectorySkillDistiller.consider(turn, signals, *, user_text) → SkillProposal|None
  - SkillCreator.create_from_proposal(proposal) → Path
启发式（对齐 Hermes）：≥5 工具调用 / 失败后成功 / 达频率阈值。
"""
from __future__ import annotations

from pathlib import Path

from Agent.evolution.skill_creator import SkillCreator
from Agent.evolution.skill_distiller import TrajectorySkillDistiller
from Agent.evolution.types import EvolutionSignal, SkillProposal
from Agent.memory.types import MemoryScope, TurnEnvelope

_SCOPE = MemoryScope(kind="personal", key="cli:user:dist1")


def _turn(tid: str = "t1") -> TurnEnvelope:
    return TurnEnvelope(
        turn_id=tid,
        session_id="s1",
        scope=_SCOPE,
        user_message_id="m",
        assistant_message_ids=(),
        tool_call_ids=tuple(f"c{i}" for i in range(6)),
        started_at=1.0,
        finished_at=2.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_distiller_emits_proposal_for_complex_success():
    """≥5 工具且无失败 → 产出带 pitfalls/verification 的 SkillProposal。"""
    distiller = TrajectorySkillDistiller(min_tools=5, frequency_threshold=1)
    signals = EvolutionSignal(
        turn_id="t1",
        completed=True,
        tool_sequence=("memory_read", "calculator", "memory_read", "calculator", "memory_edit"),
    )
    proposal = distiller.consider(
        _turn(), signals, user_text="根据记忆算一下并写回结果",
    )
    assert proposal is not None
    assert proposal.name
    assert "目标" in proposal.body or "## 目标" in proposal.body
    assert "失败处理" in proposal.body or "pitfall" in proposal.body.lower() or "失败" in proposal.body
    assert proposal.status == "draft"


def test_creator_writes_proposal_to_disk(tmp_path: Path):
    """create_from_proposal 写出 agentskills 风格 SKILL.md 并 reload。"""
    root = tmp_path / "skills"
    creator = SkillCreator(skills_root=root, frequency_threshold=99)
    proposal = SkillProposal(
        name="calc_and_remember",
        description="计算并写回记忆",
        body=(
            "## 目标\n\n计算后写回。\n\n"
            "## 步骤\n\n1. memory_read\n2. calculator\n3. memory_edit\n\n"
            "## 失败处理\n\n- 不要静默跳过\n\n"
            "## 验证\n\n- memory_read 能读到新值\n"
        ),
        status="draft",
        turn_id="t1",
    )
    path = creator.create_from_proposal(proposal)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "name: calc_and_remember" in text
    assert "status: draft" in text
    assert "## 验证" in text


def test_distiller_skips_trivial_turns():
    """工具过少且无失败克服 → 不沉淀。"""
    distiller = TrajectorySkillDistiller(min_tools=5, frequency_threshold=1)
    signals = EvolutionSignal(
        turn_id="t2",
        tool_sequence=("calculator",),
    )
    assert distiller.consider(_turn("t2"), signals, user_text="算 1+1") is None
