"""Precision 的内部分级模型与唯一对外结果。"""
from __future__ import annotations

from enum import IntEnum
from typing import Literal

from pydantic import BaseModel

from ..types import Anchor


class Priority(IntEnum):
    """固定优先级；P2 内部再区分 Anchor 与近期原文。"""

    P0 = 4
    P1 = 3
    P2 = 2
    P3 = 1


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


Decision = Literal["selected", "compressed", "lazyload", "dropped"]
AnchorStatus = Literal[
    "not_needed",
    "reused",
    "created",
    "extended",
    "raw_fallback",
]


class PrecisionRecord(BaseModel):
    """单条上下文的分级结果，供 Trace 追查。"""

    item_name: str
    priority: Priority
    token_estimate: int
    decision: Decision
    reason: str
    kind: str = ""
    role: str = ""
    content_preview: str = ""
    content_chars: int = 0
    origin_ref: str | None = None
    seq: int = 0


class PrecisionMetrics(BaseModel):
    """一轮 Precision 构建的唯一聚合指标来源。"""

    trace_id: str
    budget: int = 0
    tokens_used: int = 0
    budget_usage: float = 0.0
    message_count: int = 0
    total_chars: int = 0
    dropped_count: int = 0
    deferred_count: int = 0
    compressed_count: int = 0
    p0_dropped_count: int = 0
    p1_dropped_count: int = 0
    protected_error_missing_count: int = 0
    p3_exposed_count: int = 0
    p0_over_share: bool = False
    anchor_status: AnchorStatus = "not_needed"
    anchor_covered_turns: int = 0
    anchor_tokens: int = 0
    anchor_builder_attempts: int = 0
    anchor_error_code: str | None = None

    def event_data(self) -> dict:
        """EventBus 的稳定摘要；逐条 records 不进入事件。"""
        return self.model_dump(exclude={"trace_id", "budget", "tokens_used"})


class PrecisionResult(BaseModel):
    """PrecisionBuilder.build 的唯一返回值。"""

    messages: list[dict]
    metrics: PrecisionMetrics
    records: tuple[PrecisionRecord, ...] = ()
    exposed_refs: tuple[str, ...] = ()
    anchor: Anchor | None = None
    anchor_updated: bool = False

    @property
    def context_anchor_payload(self) -> dict | None:
        """仅在本次更新后返回可持久化的 Anchor 字典。"""
        if not self.anchor_updated or self.anchor is None:
            return None
        return self.anchor.model_dump(mode="json")

    @property
    def summary(self) -> str | None:
        """兼容旧调用方的只读渲染；新代码应持久化 Anchor。"""
        if not self.anchor_updated or self.anchor is None:
            return None
        from .percision_anchor import render_anchor

        return render_anchor(self.anchor)


class AnchorCompactionError(Exception):
    """压缩失败且原始历史也无法安全装入窗口。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        """保存稳定错误码，并保留仅供日志诊断的细节。"""
        self.code = code
        self.detail = detail
        super().__init__(detail or code)
