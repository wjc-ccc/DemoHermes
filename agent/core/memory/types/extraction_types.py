"""
AI 提取提案与确定性校验结果。

outbox extractor提取对应的卡片提案
然后由memory validator进行校验
校验通过存入memory store

"""
# 职责：定义提取提案、校验结果及已解析证据的跨模块契约。
# 边界：提案只表达后台模型的主张，不等于有效卡片或已确认事实。
# 关联：Extractor 输出提案，Reviewer/Validator 核验，Committer 使用结果入库。
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..memory_item import UserMemoryItem, make_slot_key
from .card_types import ExperienceOutcome, UserCardKind


## 记忆卡片提案 ai根据原文和现存卡片生成卡片后填槽 最后再由memory validator进行校验
class MemoryProposal(BaseModel):
    """AI 只能输出提案；是否写入由 MemoryValidator 决定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    kind: UserCardKind
    subject: str | None = None
    attribute: str | None = None
    title: str | None = None
    summary: str = Field(min_length=1)
    content: str = Field(min_length=1)
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    keywords: tuple[str, ...] = ()
    applicability: str | None = None
    action: str | None = None
    outcome: str | None = None
    limitations: str | None = None
    outcome_status: ExperienceOutcome | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source_authority: str
    source_message_ids: tuple[str, ...] = ()
    source_tool_call_ids: tuple[str, ...] = ()
    evidence_quotes: Mapping[str, str] = Field(default_factory=dict)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    reason_code: str = Field(min_length=1, max_length=100)


    ## 提案会通过对应的机械校验 后续 也考虑做一些人工校验和更正的逻辑
    ### 这里其实就是护栏三明治的逻辑  执行后自动检验hook（目前只有类别校验）
    @model_validator(mode="after")
    def _card_fields(self) -> "MemoryProposal":
        if self.kind is not UserCardKind.REUSABLE_EXPERIENCE:
            make_slot_key(self.kind, self.subject or "", self.attribute or "")
        return self

    @property
    def slot_key(self) -> str | None:
        if self.kind is UserCardKind.REUSABLE_EXPERIENCE:
            return None
        return make_slot_key(self.kind, self.subject or "", self.attribute or "")


## 记忆卡片提案校验结果
class MemoryValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    proposal_id: str
    accepted: bool
    validation_codes: tuple[str, ...]
    user_item: UserMemoryItem | None = None

### 记忆卡片提取的结果 以及对应的错误逻辑

class MemoryExtractionIssue(BaseModel):
    """单条模型输出结构非法；其他卡片仍可继续审查。"""

    model_config = ConfigDict(frozen=True)
    proposal_id: str
    code: str


class MemoryExtractionBatch(BaseModel):
    """合法空批次表示本轮没有值得沉淀的卡片。"""

    model_config = ConfigDict(frozen=True)
    proposals: tuple[MemoryProposal, ...] = ()
    issues: tuple[MemoryExtractionIssue, ...] = ()


class MemoryEvidence(BaseModel):
    """已由运行时解析并校验过的证据引用，不包含模型推理。"""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    source_type: Literal["message", "tool_call"]
    source_id: str
    source_hash: str
    excerpt: str | None = None
    observed_at: datetime
