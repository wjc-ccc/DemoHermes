"""precision / result 数据契约。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel, Field

from agent.types.precision.priority import Priority
from agent.types.context import Anchor, AdmissionVerdict, ContextBudget, TokenEstimator
from agent.types.precision.admission import AdmissionAction, AdmissionMode


@dataclass(frozen=True)
class PrecisionConfig:
    budget: ContextBudget | None = None
    token_estimator: TokenEstimator | None = None


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
    # shadow 下也记录，用来和 active 对照。
    admission_action: AdmissionAction | None = None
    admission_reason: str = ""


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
    admission_mode: AdmissionMode = "off"
    admission_candidates: int = 0
    admission_judged_count: int = 0
    admission_judge_failures: int = 0
    admission_judge_latency_ms: int = 0
    admission_rule_count: int = 0
    admission_trimmed_count: int = 0
    admission_deferred_count: int = 0
    admission_saved_tokens: int = 0
    admission_missed_count: int = 0

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
    verdicts: dict[str, AdmissionVerdict] = Field(default_factory=dict)
    verdicts_updated: bool = False

