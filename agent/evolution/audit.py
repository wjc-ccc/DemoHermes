"""EvolutionAuditEntry — 进化审计表（8.04 Phase 5.3）。

记录谁在何时对哪条 entry 做了何种 delta；SQLite 单文件，旁路观测。
"""
from __future__ import annotations

import time
import uuid

from pydantic import BaseModel, ConfigDict, Field


class EvolutionAuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    audit_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    at: float = Field(default_factory=time.time)
    actor: str
    op: str
    target: str = "memory"
    content_preview: str = ""
    turn_id: str | None = None
    reason: str = ""
    admitted: bool = True
    scope_key: str = ""
