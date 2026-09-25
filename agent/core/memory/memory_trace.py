"""MemoryTrace：记录提取、校验、持久化、召回与加载决定。"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .memory_layer import MemoryLayer
from .memory_item import UserMemoryKind


def trace_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryTraceOperation(str, Enum):
    EXTRACT = "extract"
    VALIDATE = "validate"
    PERSIST = "persist"
    RETRIEVE = "retrieve"
    LOAD = "load"


class MemoryTraceDecision(str, Enum):
    ACCEPTED = "accepted"
    MERGED = "merged"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class MemoryAiTraceFields(BaseModel):
    """只能来自 AI 结构化输出的字段，不视为已验证事实。"""

    model_config = ConfigDict(frozen=True)

    proposed_layer: MemoryLayer | None = None
    proposed_type: str | None = None
    proposed_memory_kind: UserMemoryKind | None = None
    proposed_keywords: tuple[str, ...] = ()
    self_reported_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_refs: tuple[str, ...] = ()
    proposed_mutation: str | None = None
    reason_code: str | None = None


class MemoryRuntimeTraceFields(BaseModel):
    """由运行时和 Validator 直接测得，AI 不能覆盖。"""

    model_config = ConfigDict(frozen=True)

    input_hash: str | None = None
    content_hash: str | None = None
    token_count: int | None = Field(default=None, ge=0)
    tokenizer_name: str | None = None
    validator_version: str | None = None
    dedup_candidate_ids: tuple[str, ...] = ()
    conflict_item_ids: tuple[str, ...] = ()
    validation_codes: tuple[str, ...] = ()
    query_hash: str | None = None
    keyword_hits: tuple[str, ...] = ()
    similarity_score: float | None = None
    recency_score: float | None = None
    final_relevance_score: float | None = None
    loaded_into_context: bool | None = None
    context_position: int | None = Field(default=None, ge=0)


class MemoryItemTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_item_id: str
    item_id: str | None = None
    proposal_id: str | None = None
    source_type: str | None = None
    source_ref: str | None = None
    layer: MemoryLayer | None = None
    type: str | None = None
    decision: MemoryTraceDecision
    ai: MemoryAiTraceFields = Field(default_factory=MemoryAiTraceFields)
    runtime: MemoryRuntimeTraceFields = Field(default_factory=MemoryRuntimeTraceFields)
    created_at: datetime = Field(default_factory=trace_now)


class MemoryTrace(BaseModel):
    model_config = ConfigDict(frozen=True)

    trace_id: str
    operation: MemoryTraceOperation
    session_id: str | None = None
    turn_id: str | None = None
    task_id: str | None = None
    job_id: str | None = None
    validator_version: str | None = None
    latency_ms: float | None = Field(default=None, ge=0.0)
    items: tuple[MemoryItemTrace, ...] = ()
    created_at: datetime = Field(default_factory=trace_now)
