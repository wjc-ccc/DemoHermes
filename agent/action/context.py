"""仅属于一个 turn 的可信上下文，不能挂在共享 Loop 的 current 属性上。"""
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from .policy import Toolset


class ToolContext(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    agent_id: str = "main"
    actor_id: str = ""
    role: str = "user"
    scope_key: str
    session_id: str
    session_key: str = ""
    turn_id: str
    source_message_id: str
    toolset: Toolset
    contract_hashes: dict[str, str] = Field(default_factory=dict)
    plan_id: str | None = None
    step_id: str | None = None
    segment_id: str | None = None
    memory_scope: Any = Field(default=None, exclude=True)
    skills: Any = Field(default=None, exclude=True)

    def with_skills(self, skills):
        return self.model_copy(update={"skills": skills})

