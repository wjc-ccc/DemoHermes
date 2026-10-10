"""Hermes 式 Skill 选择：按 query 相关性裁剪 L1，而非全量注入。

公共 seam：
  - SkillSelector.select(query, skills, *, top_k) → 有序 Skill 列表
  - SkillRegistry.summaries_prompt(query=..., selector=..., top_k=...) 只展示相关 active
"""
from __future__ import annotations

from pathlib import Path

from agent.skills.selector import SkillSelector
from agent.skills.registry import SkillRegistry
from agent.types.skills import Skill


def _write_skill(root: Path, name: str, description: str, *, status: str = "active") -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nstatus: {status}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_selector_ranks_by_query_overlap(tmp_path: Path):
    """与 query 词重叠更高的 skill 排在前面。"""
    root = tmp_path / "skills"
    _write_skill(root, "weekly_report", "整理一周工作周报与进展")
    _write_skill(root, "gif_search", "按关键词搜索搞笑 gif")
    _write_skill(root, "deploy_staging", "部署到 staging 服务器")
    registry = SkillRegistry(skill_dirs=[root])
    selector = SkillSelector()

    ranked = selector.select("帮我写本周工作周报", registry.active_skills(), top_k=2)
    assert [s.name for s in ranked] == ["weekly_report"]
    # top_k=2 但只有 1 个相关时不硬凑无关 skill
    ranked2 = selector.select("帮我写本周工作周报", registry.active_skills(), top_k=2, min_score=0.01)
    assert ranked2[0].name == "weekly_report"
    assert all(s.name != "gif_search" for s in ranked2)


def test_summaries_prompt_uses_selector_for_progressive_disclosure(tmp_path: Path):
    """有 query 时 L1 只含相关 skill（Hermes progressive disclosure）。"""
    root = tmp_path / "skills"
    _write_skill(root, "weekly_report", "整理一周工作周报")
    _write_skill(root, "gif_search", "搜索 gif 表情包")
    registry = SkillRegistry(skill_dirs=[root])

    prompt = registry.summaries_prompt(
        query="写周报",
        selector=SkillSelector(),
        top_k=3,
    )
    assert "weekly_report" in prompt
    assert "gif_search" not in prompt
