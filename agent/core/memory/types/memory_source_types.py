"""Session 事实进入 Memory 审查流程时使用的不可变信封。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ...types import Session
from ..memory_scope import MemoryScope


class TurnRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn_id: str
    session_id: str
    scope: MemoryScope
    user_message_id: str
    assistant_message_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    started_at: float
    finished_at: float
    completed: bool
    interrupted: bool
    error: str | None
    # Optional persisted projection of the Precision trace for this turn.
    # Kept as a plain mapping so the storage envelope does not depend on the
    # Precision implementation module.
    precision_trace: dict | None = None


class SessionReset(BaseModel):
    closed_session: Session | None
    new_session: Session
