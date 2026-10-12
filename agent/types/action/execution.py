"""仅属于一个 turn 的可信上下文，不能挂在共享 Loop 的 current 属性上。"""
from __future__ import annotations
import hashlib
import json
import time
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from agent.types.action.policy import Toolset
from agent.types.memory.scope import MemoryScope
from agent.types.skills import SkillSelection


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
    contracts: dict[str, dict] = Field(default_factory=dict)
    memory_identity: dict = Field(default_factory=dict)
    plan_source_verified: bool = False
    compensates_entry_id: str | None = None
    plan_id: str | None = None
    step_id: str | None = None
    segment_id: str | None = None
    step_lease_token: str | None = None
    target_ref: str | None = None
    target_version: str | None = None
    max_iterations: int | None = None
    memory_scope: MemoryScope | None = Field(default=None, exclude=True)
    skills: SkillSelection | None = Field(default=None, exclude=True)
    result_token_budget: int | None = Field(default=None, exclude=True)

    def with_skills(self, skills):
        return self.model_copy(update={"skills": skills})


class ActionExecutionEvidence(BaseModel):
    """单次工具事实引用的账本、领取和发布证据。"""

    model_config = ConfigDict(frozen=True)
    ledger_entry_id: str
    attempt_id: str
    status: str
    effect_state: str
    publish_state: str
    deferred_commit: bool
    lease_token: str | None = None
    replayed: bool = False
    source_message_id: str


"""可留档的守卫/闸门判定；判定不等于外部执行结果。"""


class GateDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verdict: Literal["accept", "repair", "retry", "block", "escalate", "unknown"]
    layer: Literal["syntax", "structure", "semantics", "facts", "pre", "post"]
    reasons: tuple[str, ...] = ()
    detail: str = ""
    evidence_refs: tuple[str, ...] = ()
    effective_value: dict | None = None
    retryable: bool = False
    validator_version: str = "1"


"""业务动作与每次调用尝试分离；外部效果、发布、补偿分别记录。"""


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
