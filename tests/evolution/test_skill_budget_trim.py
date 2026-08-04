"""Phase 4.2：ContextBudget 第 4 步——缩短 Skill 摘要（7.23 §17 缺口 / 8.04 P4.2）。

公共 seam：trim_to_budget(..., skill_summary_min_chars=...)
历史裁尽后仍超限 → 缩短 skill 消息；仍超则 None。
"""
from __future__ import annotations

from Agent.core.context_budget import ContextBudget, trim_to_budget


class _CharCostEstimator:
    def estimate_messages(self, messages) -> int:
        return sum(len(str(m.get("content") or "")) for m in messages)


def _skill_block(*names: str) -> str:
    header = (
        "## 可用技能（Skills）\n"
        "以下技能可显著提升特定任务的表现。\n"
    )
    lines = [f"- {n}: {'x' * 40} description for {n}" for n in names]
    return header + "\n".join(lines)


def test_skill_summary_shortened_after_history_exhausted():
    """步骤 4：历史已尽仍超预算时，缩短 skill（从尾部丢 bullet），不再直接 exceeded。"""
    skill = _skill_block("alpha", "beta", "gamma")
    tagged = [
        ("system_prompt", {"role": "system", "content": "SYS"}),
        ("memory_hot", {"role": "system", "content": "HOT"}),
        ("skill", {"role": "system", "content": skill}),
        ("history", {"role": "user", "content": "最新问题"}),
    ]
    # 永久保留 SYS+HOT+最新user + 缩短后的 skill 需能进预算
    # 全量 skill 很长；预算卡在「去掉全部 bullet 后的 header」附近
    full = sum(len(str(m["content"])) for _, m in tagged)
    header_only = (
        "## 可用技能（Skills）\n"
        "以下技能可显著提升特定任务的表现。\n"
    )
    budget_tokens = (
        len("SYS") + len("HOT") + len("最新问题") + len(header_only) + 10
    )
    assert full > budget_tokens

    budget = ContextBudget(
        model_context_tokens=budget_tokens + 50,
        reserved_output_tokens=50,
    )
    result = trim_to_budget(
        tagged, budget, _CharCostEstimator(),
        skill_summary_min_chars=len(header_only),
    )
    assert result is not None
    skill_msgs = [m for m in result if "可用技能" in str(m.get("content"))]
    assert len(skill_msgs) == 1
    content = skill_msgs[0]["content"]
    assert "gamma" not in content  # 尾部 skill 被裁
    assert "最新问题" in "\n".join(str(m.get("content")) for m in result)


def test_still_exceeded_when_skill_min_still_over_budget():
    """缩到 min 仍超限 → None（context_budget_exceeded）。"""
    skill = _skill_block("alpha")
    tagged = [
        ("system_prompt", {"role": "system", "content": "S" * 100}),
        ("skill", {"role": "system", "content": skill}),
        ("history", {"role": "user", "content": "U" * 100}),
    ]
    budget = ContextBudget(model_context_tokens=80, reserved_output_tokens=10)
    result = trim_to_budget(
        tagged, budget, _CharCostEstimator(),
        skill_summary_min_chars=20,
    )
    assert result is None
