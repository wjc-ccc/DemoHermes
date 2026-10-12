"""Perception 内部的上下文条目、历史轮次与加载阶段状态。"""
from __future__ import annotations
from dataclasses import dataclass
from pydantic import BaseModel
from agent.types.context import Anchor, AnchorFact, AdmissionVerdict

from agent.types.perception.priority import Priority


class ContextItem(BaseModel):
    """加载阶段产生、分级阶段消费的一条原子上下文。"""

    name: str
    kind: str
    priority: Priority
    message: dict
    token_estimate: int = 0
    is_error: bool = False
    origin_ref: str | None = None
    seq: int = 0
    message_id: str | None = None
    session_seq: int | None = None
    tool_fact_id: str | None = None
    tool_status: str | None = None
    tool_effect_state: str | None = None
    # 准入投影后保留 Session 原文；Anchor digest 只认原文。
    raw_message: dict | None = None


HistoryTurn = tuple[ContextItem, ...]
AnchorContent = dict[str, tuple[AnchorFact, ...]]


@dataclass(frozen=True)
class _LoadedContext:
    """load 阶段的完整产物，包含统一原子和原始历史轮次。"""

    session_id: str
    items: tuple[ContextItem, ...]
    history_turns: tuple[HistoryTurn, ...]
    anchor: Anchor | None
    legacy_anchor_content: AnchorContent | None
    anchor_error_code: str | None
    verdicts: dict[str, AdmissionVerdict]

