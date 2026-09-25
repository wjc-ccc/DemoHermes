"""装配评测：相关 Skill 进入实际模型请求，无关 Skill 不进入。"""
from __future__ import annotations

from pathlib import Path

from agent.eval import (
    EvalCase,
    EvalRunner,
    EvalSuite,
    EvalTurn,
    TurnExpectation,
)
from agent.evolution.skill_selector import SkillSelector
from agent.model.baseStructure import ModelResponse
from agent.skills import SkillRegistry


def _write_skill(root: Path, name: str, description: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nstatus: active\n---\n\n# {name}\n",
        encoding="utf-8",
    )


class _FakeModel:
    name = "fake"

    def __init__(self, text: str = "ok") -> None:
        self._text = text

    def chat(self, messages, tools=None, **kwargs):
        return ModelResponse(text=self._text)


def test_eval_skill_summary_excludes_irrelevant(tmp_path):
    """短问周报时，真实 messages 只含相关 active Skill。"""
    skills = tmp_path / "skills"
    _write_skill(skills, "weekly_report", "整理一周工作周报与进展")
    _write_skill(skills, "gif_search", "按关键词搜索搞笑 gif 动图")
    _write_skill(skills, "deploy_staging", "部署应用到 staging 集群")
    _write_skill(skills, "karaoke_score", "给 KTV 演唱打分")
    registry = SkillRegistry(skill_dirs=[skills])

    suite = EvalSuite(
        name="e3_skill_trim",
        version="1.0.0",
        cases=(
            EvalCase(
                case_id="weekly_only",
                turns=(
                    EvalTurn(
                        user_text="帮我写本周工作周报",
                        expectation=TurnExpectation(
                            skill_contains=("weekly_report",),
                            skill_lacks=("gif_search", "karaoke_score"),
                            completed=True,
                        ),
                    ),
                ),
            ),
        ),
    )
    runner = EvalRunner(
        memory_root=tmp_path / "memory",
        db_path=tmp_path / "eval.db",
        model=_FakeModel("周报草稿如下"),
        skill_registry=registry,
        skill_selector=SkillSelector(),
    )
    report = runner.run(suite)
    assert report.passed is True, report.results[0].turns[0].failures
    context = report.results[0].turns[0].model_calls[0].context_text
    assert "weekly_report" in context
    assert "gif_search" not in context


def test_eval_skill_selection_handles_no_match_draft_and_top_k(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(skills, "active_data_0", "数据分析与指标统计")
    _write_skill(skills, "active_data_1", "数据分析与指标统计")
    _write_skill(skills, "active_data_2", "数据分析与指标统计")
    _write_skill(skills, "active_data_3", "数据分析与指标统计")
    _write_skill(skills, "active_data_4", "数据分析与指标统计")
    _write_skill(skills, "active_data_5", "数据分析与指标统计")
    draft = skills / "draft_data"
    draft.mkdir(parents=True)
    (draft / "SKILL.md").write_text(
        "---\nname: draft_data\ndescription: 数据分析草稿\nstatus: draft\n---\n\n# draft\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(skill_dirs=[skills])
    suite = EvalSuite(
        name="skill_selection_edges",
        version="1.0.0",
        cases=(
            EvalCase(
                case_id="no_match",
                turns=(
                    EvalTurn(
                        user_text="给我讲一个睡前故事",
                        expectation=TurnExpectation(
                            skill_lacks=(
                                "active_data_0",
                                "draft_data",
                            ),
                        ),
                    ),
                ),
            ),
            EvalCase(
                case_id="stable_top_k",
                turns=(
                    EvalTurn(
                        user_text="分析这些数据指标",
                        expectation=TurnExpectation(
                            skill_contains=(
                                "active_data_0",
                                "active_data_4",
                            ),
                            skill_lacks=(
                                "active_data_5",
                                "draft_data",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    report = EvalRunner(
        memory_root=tmp_path / "memory",
        db_path=tmp_path / "eval.db",
        model=_FakeModel(),
        skill_registry=registry,
        skill_selector=SkillSelector(),
    ).run(suite)

    assert report.passed is True
