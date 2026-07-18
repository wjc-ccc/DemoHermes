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

from pydantic import BaseModel as PydanticModel
from pydantic import Field


class ToolCallRequest(PydanticModel):
    """一次模型发起的工具调用请求（已从各家 SDK 响应中归一化）。"""

    id: str = Field(default_factory=str, description="provider 分配的调用 id，回注 tool 消息时必须原样带回")
    name: str = Field(default_factory=str, description="工具名称")
    arguments: dict = Field(default_factory=dict, description="解析后的 JSON 参数")
    arguments_raw: str = Field(default_factory=str, description="原始参数文本（解析失败时留档排查）")


class ModelResponse(PydanticModel):
    """Loop 唯一需要理解的模型返回：文本 + 若干工具调用。"""

    text: str = Field(default_factory=str, description="模型输出的文本内容")
    tool_calls: list[ToolCallRequest] = Field(default_factory=list, description="本轮请求的工具调用列表")
    finish_reason: str = Field(default_factory=str, description="结束原因（stop / tool_calls / length ...）")
    usage: dict = Field(default_factory=dict, description="token 用量统计")
    raw: Any = Field(default=None, exclude=True, description="原始响应对象，仅调试用，不参与序列化")

    model_config = {"arbitrary_types_allowed": True}

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class BaseModel(ABC):
    """模型抽象基类。子类只需实现 chat()。"""

    name: str = "base"

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
