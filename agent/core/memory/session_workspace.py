"""调度态：只描述分工和执行状态，不调度 Agent，不判断业务是否验收。"""
from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field
from .session_state import StateRef

class WorkspaceStatus(str, Enum):
    ACTIVE = 'active'
    PAUSED = 'paused'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'

class WorkspaceItemStatus(str, Enum):
    BLOCKED = 'blocked'
    READY = 'ready'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'

class WorkspaceItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    item_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = ''
    dependencies: tuple[str, ...] = ()
    executor_ref: str = 'main'
    input_refs: tuple[StateRef, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    status: WorkspaceItemStatus = WorkspaceItemStatus.BLOCKED
    blocked_reason: str | None = None
    turn_id: str | None = None
    tool_call_ids: tuple[str, ...] = ()
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class SessionWorkspace(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    session_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    revision: int = Field(default=1, ge=1)
    plan_revision: int = Field(default=1, ge=1)
    contract_revision: int = Field(default=0, ge=0)
    current_item_id: str | None = None
    pause_reason: str | None = None
    items: tuple[WorkspaceItem, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
