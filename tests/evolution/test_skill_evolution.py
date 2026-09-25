"""SkillEvolution：落库事实 → draft → shadow replay → 正式晋升。"""
from __future__ import annotations

from pathlib import Path

from agent.storage.sqlite import RuntimeStorage
from agent.core.types import Session, ToolCall
from agent.eval import EvalRunner
from agent.evolution.skill_creator import SkillCreator
from agent.evolution.skill_distiller import TrajectorySkillDistiller
from agent.evolution.skill_evolution import SkillEvolution, suite_for_skill
from agent.evolution.skill_gate import SkillLifecycleGate
from agent.evolution.skill_optimizer import SkillOptimizer
from agent.evolution.types import SkillProposal
from agent.core.memory import MemoryScope
from agent.core.memory.types import TurnRecord
from agent.model.baseStructure import ModelResponse
from agent.skills import SkillRegistry
from agent.skills.repository import SkillRepository


_SCOPE = MemoryScope(tenant_id="cli", user_id="evo")


class _SkillAwareModel:
    name = "skill-aware"

    def __init__(self, skill_name: str) -> None:
        self._skill_name = skill_name

    def chat(self, messages, tools=None, **kwargs):
        context = "\n".join(str(message.get("content", "")) for message in messages)
        if self._skill_name in context:
            return ModelResponse(text="周报已生成")
        return ModelResponse(text="缺少周报流程")


class _Store:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_session(self, session_id: str) -> Session | None:
        if session_id != self._session.session_id:
            return None
        return self._session


def _turn() -> TurnRecord:
    return TurnRecord(
        turn_id="t-origin",
        session_id="s1",
        scope=_SCOPE,
        user_message_id="m_user",
        assistant_message_ids=("a1",),
        tool_call_ids=("c1", "c2"),
        started_at=1.0,
        finished_at=2.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_skill_evolution_keeps_draft_until_replay_improves(tmp_path: Path):
    skills_root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[skills_root])
    repo = SkillRepository(skills_root, registry=registry)
    session = Session(
        session_id="s1",
        messages=[],
        tool_calls=[
            ToolCall(
                tool_call_id="c1",
                trajectory_id="t-origin",
                tool_name="memory_read",
                status="success",
                created_at=1.0,
            ),
            ToolCall(
                tool_call_id="c2",
                trajectory_id="t-origin",
                tool_name="calculator",
                status="success",
                created_at=2.0,
            ),
        ],
    )
    from agent.core.types import Message

    session.messages.append(
        Message(message_id="m_user", role="user", content_text="帮我整理本周工作周报")
    )
    evolve = SkillEvolution(
        jobs=RuntimeStorage.open(tmp_path / "runtime.db").jobs,
        store=_Store(session),
        creator=SkillCreator(skills_root, frequency_threshold=1, registry=registry, repository=repo),
        distiller=TrajectorySkillDistiller(min_tools=5, frequency_threshold=99),
        optimizer=SkillOptimizer(registry),
        gate=SkillLifecycleGate(registry, repository=repo),
        repository=repo,
    )
    evolve.consider(_turn())
    precipitated = "memory_read_calculator"
    assert registry.get(precipitated) is not None
    assert registry.get(precipitated).status == "draft"
    assert precipitated not in registry.summaries_prompt()

    skill_name = "weekly_report_generated"
    assert evolve._creator.create_from_proposal(
        SkillProposal(
            name=skill_name,
            description="整理一周工作进展并生成周报",
            body="## 目标\n\n写周报。\n\n## 步骤\n\n1. 汇总本周事项。",
            scope_key=_SCOPE.storage_key,
            turn_id="t-origin",
        )
    ) is not None
    assert registry.get(skill_name).status == "draft"

    def factory(selected_registry: SkillRegistry) -> EvalRunner:
        return EvalRunner(
            memory_root=tmp_path / "eval-memory",
            db_path=tmp_path / "eval.db",
            model=_SkillAwareModel(skill_name),
            skill_registry=selected_registry,
        )

    evolve._runner_factory = factory
    suite = suite_for_skill(
        skill_name=skill_name,
        user_text="帮我整理本周工作周报",
        description="整理一周工作进展并生成周报",
        scope=_SCOPE,
    )
    assert evolve.evaluate_and_commit(skill_name, suite) is True
    assert registry.get(skill_name).status == "active"
    assert skill_name in registry.summaries_prompt(scope_key=_SCOPE.storage_key)
    assert skill_name not in registry.summaries_prompt(scope_key="cli:user:other")
