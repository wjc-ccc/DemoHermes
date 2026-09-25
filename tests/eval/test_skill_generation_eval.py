"""Skill 自动沉淀主链：proposal → draft → shadow Eval → replay 决策 → active。"""
from __future__ import annotations

from pathlib import Path
import shutil

from agent.eval import (
    AblationRequest,
    AblationVariant,
    EvalCase,
    EvalRunner,
    EvalSuite,
    EvalTurn,
    TurnExpectation,
    compare_reports,
    run_ablation,
)
from agent.evolution.skill_creator import SkillCreator
from agent.evolution.skill_gate import SkillLifecycleGate
from agent.evolution.types import SkillProposal
from agent.model.baseStructure import ModelResponse
from agent.skills import SkillRegistry


class _SkillAwareModel:
    name = "skill-aware"

    def __init__(self, skill_name: str) -> None:
        self._skill_name = skill_name

    def chat(self, messages, tools=None, **kwargs):
        context = "\n".join(str(message.get("content", "")) for message in messages)
        if self._skill_name in context:
            return ModelResponse(text="周报已生成")
        return ModelResponse(text="缺少周报流程")


def _suite(skill_name: str) -> EvalSuite:
    return EvalSuite(
        name="skill_weekly_report",
        version="1.0.0",
        cases=(
            EvalCase(
                case_id="generate_weekly_report",
                turns=(
                    EvalTurn(
                        user_text="帮我整理本周工作周报",
                        expectation=TurnExpectation(
                            skill_contains=(skill_name,),
                            reply_contains=("周报已生成",),
                        ),
                    ),
                ),
            ),
        ),
    )


def _runner(tmp_path: Path, name: str, registry: SkillRegistry, skill_name: str):
    return EvalRunner(
        memory_root=tmp_path / name / "memory",
        db_path=tmp_path / name / "eval.db",
        model=_SkillAwareModel(skill_name),
        skill_registry=registry,
    )


def test_generated_skill_requires_replay_improvement_before_promotion(tmp_path):
    skill_name = "weekly_report_generated"
    skills_root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[skills_root])
    creator = SkillCreator(skills_root, registry=registry)
    path = creator.create_from_proposal(
        SkillProposal(
            name=skill_name,
            description="整理一周工作进展并生成周报",
            body="# 周报流程\n\n1. 汇总本周完成事项。\n2. 列出风险和下周计划。",
            status="active",
        )
    )
    assert path is not None
    assert registry.get(skill_name).status == "draft"

    suite = _suite(skill_name)
    baseline = _runner(tmp_path, "baseline", registry, skill_name).run(suite)
    assert baseline.passed is False

    shadow_root = tmp_path / "shadow-skills"
    shutil.copytree(skills_root, shadow_root)
    shadow_registry = SkillRegistry(skill_dirs=[shadow_root])
    shadow_gate = SkillLifecycleGate(shadow_registry, success_threshold=1)
    assert shadow_gate.promote_for_replay(skill_name) is True
    candidate = _runner(
        tmp_path,
        "candidate",
        shadow_registry,
        skill_name,
    ).run(suite)
    assert candidate.passed is True

    decision = compare_reports(
        baseline,
        candidate,
        require_improvement=True,
    )
    assert decision.admitted is True
    assert decision.improved_case_ids == ("generate_weekly_report",)
    assert decision.regressed_case_ids == ()

    ablation = run_ablation(
        AblationRequest(
            baseline=candidate,
            variants=(
                AblationVariant(
                    name="without_generated_skill",
                    report=baseline,
                ),
            ),
        )
    )
    assert ablation.results[0].assessment == "keep"
    assert ablation.results[0].regressed_case_ids == ("generate_weekly_report",)

    assert SkillLifecycleGate(registry).promote_after_replay(skill_name, decision)
    assert registry.get(skill_name).status == "active"


def test_generated_skill_with_wrong_description_is_not_promoted(tmp_path):
    skill_name = "weekly_report_bad_description"
    skills_root = tmp_path / "skills"
    registry = SkillRegistry(skill_dirs=[skills_root])
    SkillCreator(skills_root, registry=registry).create_from_proposal(
        SkillProposal(
            name=skill_name,
            description="搜索搞笑动图",
            body="# 周报流程\n\n1. 汇总本周完成事项。",
        )
    )
    suite = _suite(skill_name)
    baseline = _runner(tmp_path, "baseline", registry, skill_name).run(suite)

    shadow_root = tmp_path / "shadow-skills"
    shutil.copytree(skills_root, shadow_root)
    shadow_registry = SkillRegistry(skill_dirs=[shadow_root])
    assert SkillLifecycleGate(shadow_registry).promote_for_replay(skill_name)
    candidate = _runner(
        tmp_path,
        "candidate",
        shadow_registry,
        skill_name,
    ).run(suite)

    decision = compare_reports(baseline, candidate, require_improvement=True)

    assert decision.admitted is False
    assert decision.reason == "no_improvement"
    assert SkillLifecycleGate(registry).promote_after_replay(skill_name, decision) is False
    assert registry.get(skill_name).status == "draft"
