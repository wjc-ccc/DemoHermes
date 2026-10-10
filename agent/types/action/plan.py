"""PlanStep 是调度与验收单位；完成的历史步骤不被补丁改写。"""
from __future__ import annotations
import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from agent.types.action.chain import ChainSegment


class PlanStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    step_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    toolset: str = "default"
    target_ref: str | None = None
    target_version: str | None = None
    required_effects: tuple[str, ...] = ()  # 必须成功的工具业务动作；目标步骤显式声明。
    status: Literal["todo", "ready", "doing", "done", "failed", "unknown", "blocked", "skipped"] = "todo"
    attempt: int = 0
    max_attempts: int = Field(default=3, ge=1)
    max_iterations: int = Field(default=6, ge=1)
    chain: tuple[ChainSegment, ...] = ()
    input_artifacts: dict[str, str] = Field(default_factory=dict)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    lease_token: str | None = None
    lease_expires_at: int | None = None
    error: str | None = None


class Plan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    scope_key: str
    actor_id: str
    agent_id: str = "main"
    goal: str
    source_message_id: str | None = None  # 真实用户授权来源，计划生成提示不冒充用户陈述。
    revision: int = 1
    status: Literal["pending", "running", "done", "blocked", "failed"] = "pending"
    steps: tuple[PlanStep, ...]
    contracts: dict[str, dict] = Field(default_factory=dict)
    contract_hashes: dict[str, str] = Field(default_factory=dict)


class PlanPatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    patch_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    base_revision: int
    affected_steps: tuple[str, ...]
    replace_steps: tuple[PlanStep, ...] = ()
    added_steps: tuple[PlanStep, ...] = ()
    reason: str = Field(min_length=1)
