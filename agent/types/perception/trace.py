"""perception / trace 数据契约。"""
from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field
from agent.types.perception.result import PerceptionMetrics, PerceptionRecord


class ModelCallSnapshot(BaseModel):
    """One real model attempt, including failures and unmodified provider usage."""

    model_config = ConfigDict(frozen=True)
    attempt: int = Field(ge=1)
    provider: str | None = None
    model: str | None = None
    started_at: float
    finished_at: float
    duration_ms: int = Field(ge=0)
    usage: dict = Field(default_factory=dict)
    input_token_count: int | None = Field(default=None, ge=0)
    output_token_count: int | None = Field(default=None, ge=0)
    error: str | None = None
    purpose: str = "main"
    finish_reason: str | None = None
    response_id: str | None = None
    served_model: str | None = None
    system_fingerprint: str | None = None


def normalized_usage(usage: dict) -> tuple[int | None, int | None]:
    def count(name):
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    incoming = count("prompt_tokens")
    if incoming is None:
        incoming = count("input_tokens")
        if incoming is not None:
            incoming += sum(count(name) or 0 for name in ("cache_read_input_tokens", "cache_creation_input_tokens"))
    outgoing = count("completion_tokens")
    return incoming, outgoing if outgoing is not None else count("output_tokens")


def response_metadata(response) -> dict:
    raw = getattr(response, "raw", None)
    values = {"finish_reason": getattr(response, "finish_reason", None) or None}
    for field, name in (("response_id", "id"), ("served_model", "model"), ("system_fingerprint", "system_fingerprint")):
        value = raw.get(name) if isinstance(raw, dict) else getattr(raw, name, None)
        values[field] = value if isinstance(value, str) else None
    return values


class PerceptionBuildSnapshot(BaseModel):
    """一个 ReAct 迭代的完整 Perception 输出；失败构建没有 messages。"""

    model_config = ConfigDict(frozen=True)

    iteration: int = Field(ge=1)
    messages: list[dict] | None = None
    tools: list[dict] = Field(default_factory=list)
    records: tuple[PerceptionRecord, ...] = ()
    metrics: PerceptionMetrics | None = None
    error: str | None = None
    applied_admission_message_ids: tuple[str, ...] = ()
    model_calls: tuple[ModelCallSnapshot, ...] = ()
    auxiliary_calls: tuple[ModelCallSnapshot, ...] = ()
    output_token_limit: int | None = Field(default=None, ge=1)


class PerceptionTraceSnapshot(BaseModel):
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


class PerceptionTurnTrace(BaseModel):
    """一个真实 turn 的最新 Perception 结果及其 P3 命中状态。"""

    trace_id: str
    metrics: PerceptionMetrics | None = None
    records: tuple[PerceptionRecord, ...] = ()
    exposed_refs: tuple[str, ...] = ()
    p3_hit_refs: tuple[str, ...] = ()

    builds: tuple[PerceptionBuildSnapshot, ...] = ()

    def _usage_total(self, field):
        calls = [call for build in self.builds for call in build.model_calls]
        values = [getattr(call, field) for call in calls]
        return sum(values) if values and all(value is not None for value in values) else None

    @property
    def input_token_count(self) -> int | None:
        return self._usage_total("input_token_count")

    @property
    def output_token_count(self) -> int | None:
        return self._usage_total("output_token_count")

