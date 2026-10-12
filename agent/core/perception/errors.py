"""Perception 的预算、Anchor 构建/压缩和判官错误。"""
from __future__ import annotations


class AnchorCompactionError(Exception):
    """压缩失败且原始历史也无法安全装入窗口。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        """保存稳定错误码，并保留仅供日志诊断的细节。"""
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


class ContextJudgeError(Exception):
    """判官整体不可用：网络、鉴权、协议错误。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        """保存稳定错误码；detail 不含密钥和提交的状态。"""
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


class AnchorBuildError(Exception):
    """一次 Anchor 模型调用或候选校验失败。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        """保存可供重试和 Trace 使用的稳定错误码。"""
        self.code = code
        self.detail = detail
        super().__init__(detail or code)


class ContextBudgetExceededError(Exception):
    """永远保留的部分本身已超预算：不发送请求，向上返回稳定错误码。"""

    def __init__(self, message: str = "必要上下文本身已超输入预算"):
        """创建带稳定错误码的预算异常，供 AgentLoop 转成 TurnResult。"""
        super().__init__(message)
        self.error_code = "context_budget_exceeded"
