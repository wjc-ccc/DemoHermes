"""
Tool — 工具抽象基类 + 统一执行结果

所有工具必须实现此接口：
    name        : 工具名称（如 "calculator"）
    description : 工具描述（供 LLM 理解用途）
    parameters  : JSON Schema 参数定义
    execute()   : 执行工具，返回 ToolResult

LLM 通过 to_openai_schema() 生成的 schema 决定何时、如何调用工具。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel as PydanticModel
from pydantic import Field


class ToolResult(PydanticModel):
    """工具执行的统一结果。ok=False 时 error 会被回注给 LLM 让其自我修正。"""

    ok: bool = Field(default=True, description="是否执行成功")
    result_text: str = Field(default_factory=str, description="文本结果（回注 LLM 用）")
    data: dict | None = Field(default=None, description="结构化结果（前端展示 / 程序消费用）")
    error: str | None = Field(default=None, description="失败原因")


class Tool(ABC):
    """工具抽象基类。类属性定义元信息，execute 定义行为。"""

    name: str = "base_tool"
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}

    @abstractmethod
    def execute(self, arguments: dict) -> ToolResult:
        """执行工具。实现内部应自行捕获预期内异常并返回 ToolResult(ok=False)。"""
        pass

    def to_openai_schema(self) -> dict:
        """转成 OpenAI function calling 格式，随 chat 请求发给模型。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
