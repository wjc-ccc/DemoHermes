"""Precision 使用的输入预算值对象、错误和 token 估算 seam。"""
from __future__ import annotations

from typing import Protocol, Sequence

from pydantic import BaseModel, ConfigDict, computed_field, model_validator

ERROR_CODE = "context_budget_exceeded"


class ContextBudgetExceededError(Exception):
    """永远保留的部分本身已超预算：不发送请求，向上返回稳定错误码。"""

    def __init__(self, message: str = "必要上下文本身已超输入预算"):
        """创建带稳定错误码的预算异常，供 AgentLoop 转成 TurnResult。"""
        super().__init__(message)
        self.error_code = ERROR_CODE


class ContextBudget(BaseModel):
    """模型总窗口与输出预留组成的不可变输入预算。"""

    model_config = ConfigDict(frozen=True)

    model_context_tokens: int
    reserved_output_tokens: int

    @model_validator(mode="after")
    def _check_invariant(self) -> "ContextBudget":
        """保证总窗口严格大于正数输出预留，避免产生无效输入预算。"""
        if not (self.model_context_tokens > self.reserved_output_tokens > 0):
            raise ValueError(
                "必须满足 model_context_tokens > reserved_output_tokens > 0"
            )
        return self

    @computed_field
    @property
    def max_input_tokens(self) -> int:
        """返回 Precision 真正可使用的输入额度。"""
        return self.model_context_tokens - self.reserved_output_tokens

## token估计 用于计算token消耗（或许未来英爱删减掉）
class TokenEstimator(Protocol):
    """模型 tokenizer 与测试固定成本估算器共同满足的内部 seam。"""

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        """估算一批最终模型消息占用的输入 token。"""
        ...
