"""叙事态：目标契约、只追加账本和按引用加载的工作集。与现有 Anchor 无关。"""
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from .session_state import StateRef, SessionStateItem
from .session_workspace import WorkspaceItem

class EvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    source_type: str
    source_id: str
    source_revision: str | None = None
    source_path: str | None = None
    source_hash: str | None = None
    excerpt: str | None = None
    observed_at: datetime | None = None

class GoalContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    session_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    user_goal: str = Field(min_length=1)
    success_criteria: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    source_message_id: str = Field(min_length=1)
    changed_by: str = Field(min_length=1)
    change_reason: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class NarrativeItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    item_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    sequence_no: int | None = None
    workspace_item_id: str | None = None
    what_happened: str = Field(min_length=1)
    decision: str | None = None
    reason: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    state_delta_refs: tuple[StateRef, ...] = ()
    next_action: str | None = None
    author: str = Field(min_length=1)
    status: Literal['recorded', 'proposed'] = 'recorded'
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class WorkingSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    session_id: str
    contract: GoalContract | None = None
    current_item: WorkspaceItem | None = None
    events: tuple[NarrativeItem, ...] = ()
    state_items: tuple[SessionStateItem, ...] = ()

class SessionNarrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    session_id: str
    contract: GoalContract | None = None
    events: tuple[NarrativeItem, ...] = ()
