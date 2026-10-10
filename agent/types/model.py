"""model 数据契约。"""
from __future__ import annotations
from typing import Any
from pydantic import BaseModel as PydanticModel, Field


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
