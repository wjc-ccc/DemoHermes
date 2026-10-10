"""运行时顺手记录的观测；恢复事实以持久账本为准。"""
from __future__ import annotations
from pydantic import BaseModel, Field


class ActionTrace(BaseModel):
    trace_id: str
    ledger_entry_ids: tuple[str, ...] = ()
    attempt_ids: tuple[str, ...] = ()
    decisions: tuple[dict, ...] = ()
    # 模型给出的解释只是提案，不作为执行成功或来源可信的证据。
    model_reasons: dict[str, str] = Field(default_factory=dict)
