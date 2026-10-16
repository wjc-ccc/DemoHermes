"""
BaseModel — 模型抽象基类 + 统一响应结构

所有 LLM provider 必须实现此接口，对上层（core/loop）隐藏具体 API 差异：
    chat(messages, tools=None) -> ModelResponse

messages 格式遵循 OpenAI 标准：[{"role": "user", "content": "..."}]
tools    格式遵循 OpenAI function calling：[{"type": "function", "function": {...}}]

设计要点：
    - Loop 不直接解析各家 SDK 的响应对象，只认 ModelResponse / ToolCallRequest
    - 是否支持工具调用由具体 provider 决定；不支持时返回纯文本即可
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


from agent.types.model import ModelResponse


class BaseModel(ABC):
    """模型抽象基类。子类只需实现 chat()。"""

    name: str = "base"
    supports_images: bool = False

    def __init__(self) -> None:
        """加载对应的 client，并进行初始化。"""
        super().__init__()

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> ModelResponse:
        """
        发送消息列表，返回统一 ModelResponse。

        :param messages: OpenAI 格式消息列表
        :param tools:    OpenAI function calling 格式的工具 schema 列表；None 表示本轮不允许调工具
        """
        pass
