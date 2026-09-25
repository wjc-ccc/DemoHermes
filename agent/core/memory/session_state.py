"""机械态：程序提供的精确值和来源，不接入工具执行或模型上下文。"""
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class StateRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    session_id: str = Field(min_length=1)
    scope_key: str = Field(min_length=1)
    key: str = Field(min_length=1)
    revision: int = Field(ge=1)

class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    source_type: Literal['tool_output', 'user_input', 'derived']
    source_id: str = Field(min_length=1)
    source_path: str | None = None
    parent_refs: tuple[StateRef, ...] = ()

class StateValidity(str, Enum):
    VALID = 'valid'
    INVALIDATED = 'invalidated'

class SessionStateItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    item_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    scope_key: str = Field(min_length=1)
    key: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    value_type: Literal['string', 'integer', 'decimal', 'boolean', 'object', 'array', 'null']
    value: Any
    validity: StateValidity = StateValidity.VALID
    provenance: Provenance
    workspace_item_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = Field(min_length=1)
    invalidation_reason: str | None = None

    @property
    def ref(self) -> StateRef:
        return StateRef(session_id=self.session_id, scope_key=self.scope_key,
                        key=self.key, revision=self.revision)

class SessionState(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    session_id: str
    items: tuple[SessionStateItem, ...] = ()
