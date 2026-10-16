"""主循环配置。每个可调类别只有一个 XxxConfig，这里是 LoopConfig。"""
from __future__ import annotations

from dataclasses import dataclass


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须为正整数")


def _non_negative_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是大于等于 0 的整数")


@dataclass(frozen=True)
class LoopConfig:
    """一次用户回合里，主模型最多想几步，以及每条工具结果最多占多少输入。"""

    max_iterations: int = 8
    model_retries: int = 2
    agent_id: str = "main"
    tool_result_min_remaining: int = 256
    tool_result_reserve: int = 512
    tool_result_floor: int = 128
    tool_result_cap: int = 2048

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id 不能为空")
        _positive_int("max_iterations", self.max_iterations)
        _non_negative_int("model_retries", self.model_retries)
        _non_negative_int("tool_result_reserve", self.tool_result_reserve)
        for name in ("tool_result_min_remaining", "tool_result_floor", "tool_result_cap"):
            _positive_int(name, getattr(self, name))
        if self.tool_result_floor > self.tool_result_cap:
            raise ValueError("tool_result_floor 不能大于 tool_result_cap")

    def tool_result_limit(self, *, input_tokens: int, tokens_used: int, call_count: int) -> int:
        """按本轮剩余输入预算，给这一批工具结果分配单条上限。"""
        remaining = max(
            self.tool_result_min_remaining,
            input_tokens - tokens_used - self.tool_result_reserve,
        )
        share = remaining // (2 * max(1, call_count))
        return max(self.tool_result_floor, min(self.tool_result_cap, share))
