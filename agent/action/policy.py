"""工具契约与本次授权；只配置能力，不保存运行状态。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class ToolPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    when_to_use: str = Field(min_length=1)
    when_not_to_use: str = Field(min_length=1)
    source: Literal["builtin", "skill", "mcp"] = "builtin"
    effect: Literal["read", "write", "external"] = "read"
    risk: Literal["low", "medium", "high", "critical"] = "low"
    permission: Literal["any", "user", "owner", "admin"] = "any"
    idempotency: Literal["read_only", "idempotent", "keyed", "non_idempotent"] = "read_only"
    key_fields: tuple[str, ...] = ()
    precondition_field: str | None = None
    max_calls_per_turn: int = Field(default=8, ge=1)
    max_attempts: int = Field(default=3, ge=1)
    max_recovery_queries: int = Field(default=8, ge=1)
    timeout_s: float = Field(default=30, gt=0)
    requires_approval: bool = False
    recovery_tool: str | None = None
    compensation_tool: str | None = None
    contract_version: str = "1"
    implementation_version: str = "1"
    deferred_commit: bool = False

    @model_validator(mode="after")
    def validate_effect(self):
        if self.effect != "read" and self.idempotency == "read_only":
            raise ValueError("write/external 工具不能声明 read_only")
        if self.idempotency == "keyed" and not self.key_fields:
            raise ValueError("keyed 工具必须声明业务 key_fields")
        if self.effect == "external" and self.idempotency == "non_idempotent":
            raise ValueError("external non_idempotent 不允许自动执行")
        return self


class Toolset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = "default"
    tools: tuple[str, ...] = ()
    max_calls: dict[str, int] = Field(default_factory=dict)
    max_risk: Literal["low", "medium", "high", "critical"] = "medium"

