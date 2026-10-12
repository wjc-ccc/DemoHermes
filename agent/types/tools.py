"""tools 数据契约。"""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Literal, Protocol
from pydantic import BaseModel as PydanticModel, Field
from agent.types.action.execution import ActionItem, ActionAttemptItem, ToolContext
from agent.types.action.policy import ToolPolicy
from agent.types.memory.mutation import MemoryMutationItem
from agent.types.memory.scope import MemoryScope
from agent.types.model import ToolCallRequest
from agent.types.runtime import TurnContext
from agent.types.skills import SkillSelection


class ToolCallResult(PydanticModel):
    """工具执行的统一结果。ok=False 时 error 会被回注给 LLM 让其自我修正。"""

    ok: bool = Field(default=True, description="是否执行成功")
    result_text: str = Field(default_factory=str, description="文本结果（回注 LLM 用）")
    data: dict | None = Field(default=None, description="结构化结果（前端展示 / 程序消费用）")
    error: str | None = Field(default=None, description="失败原因")
    pending_mutation: MemoryMutationItem | None = None
    readback_reference: str | None = None


@dataclass(frozen=True)
class ToolInvocationContext:
    session_id: str = ""
    turn_id: str = ""
    session_key: str = ""
    source_message_id: str | None = None
    memory_scope: MemoryScope | None = None
    skills: SkillSelection | None = None
    result_token_budget: int | None = None


@dataclass(frozen=True)
class ToolTurnContext:
    turn: TurnContext
    invocation: ToolInvocationContext
    available_tools: tuple[str, ...] | None
    max_iterations: int | None = None
    action: ToolContext | None = None

    @classmethod
    def from_action(cls, action: ToolContext) -> ToolTurnContext:
        turn = TurnContext(
            session_id=action.session_id, turn_id=action.turn_id,
            source_message_id=action.source_message_id, session_key=action.session_key,
            agent_id=action.agent_id, author_id=action.actor_id, memory_scope=action.memory_scope,
        )
        return cls(
            turn=turn,
            invocation=ToolInvocationContext(
                session_id=action.session_id, turn_id=action.turn_id,
                session_key=action.session_key, source_message_id=action.source_message_id,
                memory_scope=action.memory_scope, skills=action.skills,
            ),
            available_tools=tuple(action.toolset.tools), max_iterations=action.max_iterations,
            action=action,
        )

    def with_skills(self, skills: SkillSelection | None) -> ToolTurnContext:
        return replace(
            self, invocation=replace(self.invocation, skills=skills),
            action=self.action.with_skills(skills) if self.action is not None else None,
        )


@dataclass(frozen=True)
class ToolDispatchItem:
    request: ToolCallRequest
    invocation: ToolInvocationContext
    effective_args: dict
    fact_id: str
    started_at: float
    mode: Literal["invoke", "replay", "return"] = "invoke"
    context: ToolContext | None = None
    action_item: ActionItem | None = None
    attempt: ActionAttemptItem | None = None
    policy: ToolPolicy | None = None
    result: ToolCallResult | None = None
    replayed: bool = False


class ToolDefinition(Protocol):
    """工具装配需要的定义；不依赖具体 Tool 基类。"""

    name: str
    description: str
    parameters: dict

    def to_openai_schema(self) -> dict: ...
    def execute(self, arguments: dict, *, context: ToolInvocationContext) -> ToolCallResult: ...
