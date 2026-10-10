"""precision / trace 数据契约。"""
from __future__ import annotations
from agent.types.precision.result import PrecisionMetrics, PrecisionRecord
from pydantic import BaseModel, ConfigDict, Field
from agent.types.precision.result import PrecisionMetrics, PrecisionRecord


class PrecisionBuildSnapshot(BaseModel):
    """一个 ReAct 迭代的完整 Precision 输出；失败构建没有 messages。"""

    model_config = ConfigDict(frozen=True)

    iteration: int = Field(ge=1)
    messages: list[dict] | None = None
    tools: list[dict] = Field(default_factory=list)
    records: tuple[PrecisionRecord, ...] = ()
    metrics: PrecisionMetrics | None = None
    error: str | None = None


class PrecisionTraceSnapshot(BaseModel):
    """滚动窗口的长尾压力、关键层丢失和 P3 回取率。"""

    n: int
    budget_usage_p95: float
    budget_usage_p99: float
    dropped_count_p95: float
    dropped_count_p99: float
    deferred_count_p95: float
    deferred_count_p99: float
    p0_dropped_count: int
    p1_dropped_count: int
    protected_error_missing_count: int
    p3_hit_rate: float


class PrecisionTurnTrace(BaseModel):
    """一个真实 turn 的最新 Precision 结果及其 P3 命中状态。"""

    trace_id: str
    metrics: PrecisionMetrics | None = None
    records: tuple[PrecisionRecord, ...] = ()
    exposed_refs: tuple[str, ...] = ()
    p3_hit_refs: tuple[str, ...] = ()

    builds: tuple[PrecisionBuildSnapshot, ...] = ()

