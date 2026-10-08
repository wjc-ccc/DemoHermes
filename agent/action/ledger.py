"""业务动作与每次调用尝试分离；外部效果、发布、补偿分别记录。"""
import hashlib
import json
import time
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


def now_ms():
    return int(time.time() * 1000)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class ActionItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    entry_id: str
    idempotency_key: str
    tool_name: str
    operation: str = ""
    scope_key: str
    context: dict = Field(default_factory=dict)
    contract_hash: str = ""
    contract_version: str = "1"
    effect: Literal["read", "write", "external"] = "read"
    effect_state: Literal["not_started", "started", "succeeded", "failed", "unknown"] = "not_started"
    phase: Literal["pending", "blocked_pre", "prepared", "finished"] = "pending"
    publish_state: Literal["pending", "accepted", "redacted", "blocked"] = "pending"
    compensation_state: Literal["not_requested", "started", "succeeded", "failed", "unknown"] = "not_requested"
    input_hash: str = ""
    requested_args: dict = Field(default_factory=dict)
    effective_args: dict = Field(default_factory=dict)
    raw_output: dict | None = None
    publishable_output: dict | None = None
    pre_decision: dict | None = None
    post_decision: dict | None = None
    resource_ref: str | None = None
    resource_version: str | None = None
    external_ref: str | None = None
    revision: int = 1
    attempt_count: int = 0
    recovery_count: int = 0
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: int | None = None
    created_at: int = Field(default_factory=now_ms)
    updated_at: int = Field(default_factory=now_ms)

    @property
    def status(self):
        if self.compensation_state == "succeeded":
            return "compensated"
        if self.phase == "blocked_pre":
            return "blocked_pre"
        if self.publish_state == "blocked":
            return "blocked_post"
        return self.effect_state


class ActionAttemptItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    attempt_id: str
    entry_id: str | None = None
    tool_call_id: str
    tool_name: str
    scope_key: str
    turn_id: str
    requested_args: dict
    admitted: bool = False
    state: str = "not_started"
    reason: str | None = None
    created_at: int = Field(default_factory=now_ms)
    finished_at: int | None = None

