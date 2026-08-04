"""
整轮 ContextBudget（7.23 方案 §4.3）

Hot 字符硬上限（Agent/memory/budget.py）不能替代模型上下文预算；
这里管的是发给模型的最终 messages，两类预算不得混淆。

- ContextBudget：model_context_tokens > reserved_output_tokens > 0 是构造不变量；
  max_input_tokens 只能派生，调用方不能传入第三个可能不一致的数。
- TokenEstimator：外部 seam，测试用 Fake 返回固定字面量成本；
  未提供 tokenizer 时用保守估算（允许过度裁剪，不允许乐观低估）。
- 裁剪以协议组为单位、顺序固定、结果确定性相同；
  仍超限返回 context_budget_exceeded，不发送已破坏协议的请求。
"""
from __future__ import annotations

from typing import Protocol, Sequence

from pydantic import BaseModel, ConfigDict, computed_field, model_validator

ERROR_CODE = "context_budget_exceeded"


class ContextBudgetExceededError(Exception):
    """永远保留的部分本身已超预算：不发送请求，向上返回稳定错误码。"""

    def __init__(self, message: str = "必要上下文本身已超输入预算"):
        super().__init__(message)
        self.error_code = ERROR_CODE


class ContextBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_context_tokens: int
    reserved_output_tokens: int

    @model_validator(mode="after")
    def _check_invariant(self) -> "ContextBudget":
        if not (self.model_context_tokens > self.reserved_output_tokens > 0):
            raise ValueError(
                "必须满足 model_context_tokens > reserved_output_tokens > 0"
            )
        return self

    @computed_field
    @property
    def max_input_tokens(self) -> int:
        return self.model_context_tokens - self.reserved_output_tokens


class TokenEstimator(Protocol):
    def estimate_messages(self, messages: Sequence[dict]) -> int: ...


# ---- 裁剪（确定性：相同输入与预算 → 相同结果）----

# tagged item = (kind, message)；kind 由 ContextBuilder 在组装时标记
_TAG_SYSTEM = "system_prompt"
_TAG_MEMORY_HOT = "memory_hot"
_TAG_SKILL = "skill"
_TAG_EPHEMERAL = "ephemeral"
_TAG_HISTORY = "history"


def _shorten_provider_message(message: dict, min_chars: int) -> dict:
    """缩短 Provider recall 到其最小配额：保留围栏行，inner 裁到 min_chars。

    只处理 Builder 结构化注入的 <provider-memory> 围栏块；无法解析时保持原样。
    """
    content = str(message.get("content") or "")
    lines = content.split("\n")
    if len(lines) < 3:
        return message
    opening, closing = lines[0], lines[-1]
    inner = "\n".join(lines[1:-1])
    return {**message, "content": f"{opening}\n{inner[:max(0, min_chars)]}\n{closing}"}


def _shorten_skill_message(message: dict, min_chars: int) -> dict:
    """缩短 Skill Level-1 摘要：从尾部丢 bullet，至少保留 min_chars 的前缀。

    格式约定见 SkillRegistry.summaries_prompt（标题 + 说明 + `- name:` 行）。
    """
    content = str(message.get("content") or "")
    if len(content) <= min_chars:
        return {**message, "content": content}
    lines = content.splitlines()
    bullets = [i for i, line in enumerate(lines) if line.startswith("- ")]
    # 逐个从尾部去掉 skill bullet，直到 ≤ 预算由调用方判定；此处先生成最短候选
    kept = list(lines)
    drop_from = len(bullets) - 1
    while drop_from >= 0:
        idx = bullets[drop_from]
        kept = kept[:idx] + kept[idx + 1 :]
        candidate = "\n".join(kept).rstrip() + ("\n" if content.endswith("\n") else "")
        # 中间态也返回；调用方按 cost 决定是否继续。这里一次性缩到 min。
        if len(candidate) <= min_chars or drop_from == 0:
            if len(candidate) > min_chars:
                candidate = candidate[:max(0, min_chars)].rstrip()
            return {**message, "content": candidate}
        drop_from -= 1
    return {**message, "content": content[:max(0, min_chars)]}


def trim_to_budget(
    tagged: list[tuple[str, dict]],
    budget: ContextBudget,
    estimator: TokenEstimator,
    provider_min_recall_chars: int | None = None,
    skill_summary_min_chars: int | None = None,
) -> list[dict] | None:
    """按 §4.3 固定顺序裁剪；仍超限返回 None（调用方报 context_budget_exceeded）。

    永远保留：system prompt、ephemeral、Hot 记忆、最新 user message、
    当前迭代的 assistant tool-call 与全部 tool results（最后一条 user 起的尾部）。
    裁剪单位是完整历史 turn（user 起到下一 user 前），协议组不会被拆开。
    """
    items = list(tagged)

    def cost(xs: list[tuple[str, dict]]) -> int:
        return estimator.estimate_messages([m for _, m in xs])

    if cost(items) <= budget.max_input_tokens:
        return [m for _, m in items]

    # 步骤 1：移除可选 Daily（ADR-4：daily 默认按需，最先让位）
    items = [t for t in items if t[0] != "memory_daily"]
    if cost(items) <= budget.max_input_tokens:
        return [m for _, m in items]

    # 步骤 2：缩短 Provider recall 到其最小配额（配置了才生效；先于动历史）
    if provider_min_recall_chars is not None:
        items = [
            (k, _shorten_provider_message(m, provider_min_recall_chars)
             if k == "memory_provider" else m)
            for k, m in items
        ]
        if cost(items) <= budget.max_input_tokens:
            return [m for _, m in items]

    conv = [i for i, (k, _) in enumerate(items) if k == _TAG_HISTORY]
    user_idx = [i for i in conv if items[i][1].get("role") == "user"]
    if not conv or not user_idx:
        # 无历史可裁：仍可走步骤 4
        pass
    else:
        last_user = user_idx[-1]

        # 历史 turn 分组：会话起点（或一条 user）到下一个 user 之前，从旧到新
        head = conv[0]
        splits = [p for p in user_idx if head < p < last_user]
        bounds = [head, *splits, last_user]
        groups = [list(range(bounds[i], bounds[i + 1])) for i in range(len(bounds) - 1)]

        removed: set[int] = set()
        for group in groups:  # 步骤 3：从最旧开始移除完整历史 turn
            removed.update(group)
            kept = [t for i, t in enumerate(items) if i not in removed]
            if cost(kept) <= budget.max_input_tokens:
                return [m for _, m in kept]
        items = [t for i, t in enumerate(items) if i not in removed]

    # 步骤 4：缩短 Skill 摘要（§17 缺口；配置了 min 才做）
    if skill_summary_min_chars is not None:
        # 先逐条丢 bullet 再硬截到 min，中间每步检查预算
        skill_indices = [i for i, (k, _) in enumerate(items) if k == _TAG_SKILL]
        for si in skill_indices:
            kind, msg = items[si]
            content = str(msg.get("content") or "")
            lines = content.splitlines()
            bullet_idxs = [i for i, line in enumerate(lines) if line.startswith("- ")]
            # 从尾部逐个去掉 bullet
            for drop_i in range(len(bullet_idxs) - 1, -1, -1):
                bi = bullet_idxs[drop_i]
                lines = lines[:bi] + lines[bi + 1 :]
                candidate = "\n".join(lines)
                items[si] = (kind, {**msg, "content": candidate})
                if cost(items) <= budget.max_input_tokens:
                    return [m for _, m in items]
            # 仍超：硬截到 min_chars
            shortened = _shorten_skill_message(
                items[si][1], skill_summary_min_chars,
            )
            items[si] = (kind, shortened)
            if cost(items) <= budget.max_input_tokens:
                return [m for _, m in items]

    return None
