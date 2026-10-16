"""perception / result 数据契约。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel, Field

from agent.types.perception.priority import Priority
from agent.types.context import Anchor, AdmissionVerdict, ContextBudget, TokenEstimator
from agent.types.perception.admission import AdmissionAction, AdmissionMode


@dataclass(frozen=True)
class PerceptionConfig:
    """感知配置：窗口、压缩时机、保留轮数和 Anchor 上限。"""

    budget: ContextBudget | None = None
    token_estimator: TokenEstimator | None = None
    compression_trigger_tokens: int | None = None
    anchor_enabled: bool = True
    # 默认 None。有物理窗口且未开软触发时，上限为输入预算 × anchor_budget_ratio。
    # 软触发下显式传入 None 时，不另设语义输出上限。
    anchor_output_token_limit: int | None = None
    # 独立于语义压缩目标，限制实际Anchor模型调用的输出量。
    anchor_provider_output_limit: int = 4096
    anchor_reserve_tokens: int | None = None
    compact_ratio: float = 0.85
    keep_recent_turns: int = 2
    p0_share_warning: float = 0.10
    anchor_budget_ratio: float = 0.15

    def __post_init__(self) -> None:
        """软触发和规划预留不改变模型物理窗口；比例与可选 token 配置分开校验。"""
        if (isinstance(self.anchor_provider_output_limit, bool)
                or not isinstance(self.anchor_provider_output_limit, int)
                or self.anchor_provider_output_limit <= 0):
            raise ValueError("anchor_provider_output_limit 必须为正整数")
        for name in ("compression_trigger_tokens", "anchor_output_token_limit", "anchor_reserve_tokens"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} 必须为正整数或 None")
        for name in ("compact_ratio", "p0_share_warning", "anchor_budget_ratio"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= 1:
                raise ValueError(f"{name} 必须在 (0, 1] 内")
        if isinstance(self.keep_recent_turns, bool) or not isinstance(self.keep_recent_turns, int) or self.keep_recent_turns < 0:
            raise ValueError("keep_recent_turns 必须是大于等于 0 的整数")


Decision = Literal["selected", "compressed", "lazyload", "dropped"]


AnchorStatus = Literal[
    "not_needed",
    "reused",
    "created",
    "extended",
    "raw_fallback",
]


class PerceptionRecord(BaseModel):
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


class PerceptionMetrics(BaseModel):
    """一轮 Perception 构建的唯一聚合指标来源。"""

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


class PerceptionResult(BaseModel):
    """PerceptionBuilder.build 的唯一返回值。"""

    messages: list[dict]
    metrics: PerceptionMetrics
    records: tuple[PerceptionRecord, ...] = ()
    exposed_refs: tuple[str, ...] = ()
    anchor: Anchor | None = None
    anchor_updated: bool = False
    verdicts: dict[str, AdmissionVerdict] = Field(default_factory=dict)
    verdicts_updated: bool = False
    applied_admission_message_ids: tuple[str, ...] = ()
    output_token_limit: int | None = Field(default=None, ge=1)

