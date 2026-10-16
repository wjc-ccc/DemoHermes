"""
Tool — 工具抽象基类 + 统一执行结果

所有工具必须实现此接口：
    name        : 工具名称（如 "calculator"）
    description : 工具描述（供 LLM 理解用途）
    parameters  : JSON Schema 参数定义
    execute()   : 执行工具，返回 ToolCallResult

LLM 通过 to_openai_schema() 生成的 schema 决定何时、如何调用工具。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING


from agent.types.tools import ToolCallResult, ToolInvocationContext

if TYPE_CHECKING:
    from agent.types.action.policy import ToolPolicy


class Tool(ABC):
    """工具抽象基类。类属性定义元信息，execute 定义行为。"""

    name: str = "base_tool"
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}

    # action 控制层的定级由工具自己声明，装配处只做校验不代写：
    # 未声明 action_policy 的工具不能进入受控执行路径。
    action_policy: ToolPolicy | None = None
    # 按 operation/action 细分的定级；键与参数里的 operation/action 取值一致。
    action_operations: dict[str, ToolPolicy] | None = None

    @abstractmethod
    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult:
        """执行工具。实现内部应自行捕获预期内异常并返回 ToolCallResult(ok=False)。"""
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
