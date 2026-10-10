"""工具暂存的卡片修改指令；实际写入由轮次事务确认。"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from agent.types.memory.categories import ExperienceOutcome


class MemoryMutationItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["update", "delete"]
    item_id: str
    expected_revision: int
    summary: str = ""
    content: str = ""
    title: str | None = None
    keywords: list[str] = Field(default_factory=list)
    applicability: str | None = None
    action: str | None = None
    outcome: str | None = None
    limitations: str | None = None
    outcome_status: ExperienceOutcome | None = None
    reason_code: str = "model_memory_edit"

    def command_payload(self) -> dict:
        """卡片命令的传输形状；删除只有定位与版本字段。"""
        value = self.model_dump(mode="json")
        if self.operation == "delete":
            return {name: value[name] for name in ("operation", "item_id", "expected_revision")}
        return value

    @field_validator("operation", mode="before")
    @classmethod
    def require_supported_operation(cls, value):
        if value not in {"update", "delete"}:
            raise ValueError("unsupported memory mutation operation or fields")
        return value
