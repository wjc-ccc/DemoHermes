"""E3：装配评测——无关 skill 不进 L1 摘要（8.15）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Agent.eval import EvalAssertion, EvalCase, EvalRunner, EvalSuite
from Agent.evolution.skill_selector import SkillSelector
from Agent.model.baseStructure import ModelResponse
from Agent.skills import SkillRegistry


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
    """Hot 偏好 + 大量无关 skill + 短问周报 → L1 含 weekly_report，不含 gif_search。"""
    skills = tmp_path / "skills"
    _write_skill(skills, "weekly_report", "整理一周工作周报与进展")
    _write_skill(skills, "gif_search", "按关键词搜索搞笑 gif 动图")
    _write_skill(skills, "deploy_staging", "部署应用到 staging 集群")
    _write_skill(skills, "karaoke_score", "给 KTV 演唱打分")
    registry = SkillRegistry(skill_dirs=[skills])

    # 全量摘要应明显更长；有 selector 后应显著变瘦
    full = registry.summaries_prompt() or ""
    selected = registry.summaries_prompt(
        query="帮我写本周工作周报",
        selector=SkillSelector(),
    ) or ""
    assert "weekly_report" in selected
    assert "gif_search" not in selected
    assert len(selected) < len(full)

    suite = EvalSuite(
        name="e3_skill_trim",
        cases=(
            EvalCase(
                name="weekly_only",
                user_text="帮我写本周工作周报",
                assertions=(
                    EvalAssertion(kind="skill_summary_contains", value="weekly_report"),
                    EvalAssertion(kind="skill_summary_lacks", value="gif_search"),
                    EvalAssertion(kind="skill_summary_lacks", value="karaoke_score"),
                    EvalAssertion(
                        kind="max_skill_summary_chars",
                        value=len(full) - 1,
                    ),
                    EvalAssertion(kind="completed", value=True),
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
    assert report.passed is True, report.results[0].failures
    assert report.results[0].skill_summary_chars < len(full)
