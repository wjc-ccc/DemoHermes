"""
AI 提取提案与确定性校验结果。

outbox extractor提取对应的卡片提案 
然后由memory validator进行校验 
校验通过存入memory store

"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...types import Session
from ..memory_layer import MemoryLayer
from ..session_narrative import NarrativeItem
from ..memory_item import UserMemoryKind, UserMemoryItem
from .memory_source_types import TurnRecord


class ProposedMutation(str, Enum):
    CREATE = "create"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    NOOP = "noop"

## 记忆卡片提案 
class MemoryProposal(BaseModel):
    """AI 只能输出提案；是否写入由 MemoryValidator 决定。"""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    target_layer: Literal[MemoryLayer.USER, MemoryLayer.TASK]
    type: str = Field(min_length=1, max_length=64)
    memory_kind: UserMemoryKind | None = None
    normalized_key: str = Field(min_length=1, max_length=200)
    title: str | None = None
    summary: str = Field(min_length=1)
    content: str = Field(min_length=1)
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    keywords: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    source_authority: str
    source_message_ids: tuple[str, ...] = ()
    source_tool_call_ids: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    proposed_mutation: ProposedMutation = ProposedMutation.CREATE
    supersedes_item_id: str | None = None
    reason_code: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _layer_specific_fields(self) -> "MemoryProposal":
        if self.target_layer is MemoryLayer.TASK and self.memory_kind is not None:
            raise ValueError("task 提案不能携带 memory_kind")
        if self.proposed_mutation is ProposedMutation.SUPERSEDE and not self.supersedes_item_id:
            raise ValueError("supersede 提案必须提供 supersedes_item_id")
        return self


## 记忆卡片提案校验结果
class MemoryValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    proposal_id: str
    accepted: bool
    validation_codes: tuple[str, ...]
    user_item: UserMemoryItem | None = None
    narrative_item: NarrativeItem | None = None


class MemoryEvidence(BaseModel):
    """已由运行时解析并校验过的证据引用，不包含模型推理。"""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    source_type: Literal["message", "tool_call"]
    source_id: str
    source_hash: str
    observed_at: datetime


class MemoryExtractor(Protocol):
    def extract(self, turn: TurnRecord, session: Session) -> list[MemoryProposal]: ...
