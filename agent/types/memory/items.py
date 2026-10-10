"""跨来源统一的轻量 MemoryItem 投影。"""
from __future__ import annotations
import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Any, Literal, Mapping
from pydantic import BaseModel, ConfigDict, Field, model_validator
from agent.types.memory.layers import MemoryLayer, validate_item_type
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import EvidenceRef
from agent.types.memory.categories import ExperienceOutcome, UserCardKind, UserCardStatus


# 职责：定义上下文原子投影和结构化 User 卡，生成稳定事实的 slot_key。
# 边界：模型只负责提出 subject/attribute；这里不负责证据核验或 SQLite 写入。
# 关联：Validator 创建 UserMemoryItem，memory_store.py 负责持久化。


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


_SLOT_ALIASES = {
    "用户": "user", "我": "user", "user": "user", "用户本人": "user", "本人": "user", "me": "user",
    "助手": "assistant", "回复": "assistant", "assistant": "assistant",
    "语言": "response_language", "回复语言": "response_language",
    "response_language": "response_language", "language": "response_language",
    "详细程度": "response_detail", "回复详细程度": "response_detail",
    "response_detail": "response_detail", "detail": "response_detail",
    "姓名": "display_name", "名字": "display_name", "display_name": "display_name",
}


def _slot_part(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = _SLOT_ALIASES.get(value, value)
    return re.sub(r"[^\w\u3400-\u9fff]+", "_", value).strip("_")[:80]


def make_slot_key(kind: UserCardKind, subject: str, attribute: str) -> str:
    """由运行时生成稳定事实键；事实的具体取值不进入键名。"""
    if kind is UserCardKind.REUSABLE_EXPERIENCE:
        raise ValueError("经验观察不占用事实槽位")
    entity, facet = _slot_part(subject), _slot_part(attribute)
    if not entity or not facet:
        raise ValueError("事实必须给出明确的对象和属性")
    return f"{kind.value}:{entity}:{facet}"


class MemoryItem(BaseModel):
    """观测、召回和上下文候选共用的投影，不是万能持久化实体。"""

    model_config = ConfigDict(frozen=True)

    item_id: str
    layer: MemoryLayer
    type: str
    source_type: str
    source_ref: str
    scope: MemoryScope = Field(default_factory=MemoryScope)
    title: str | None = None
    content: str
    summary: str | None = None
    priority: int = Field(default=50, ge=0, le=100)
    token_count: int = Field(default=0, ge=0)
    content_hash: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_layer_and_hash(self) -> "MemoryItem":
        validate_item_type(self.layer, self.type)
        if self.content_hash != content_sha256(self.content):
            raise ValueError("content_hash 与 content 不一致")
        return self


"""Policy 层原子文档投影。"""

class PolicyItem(MemoryItem):
    layer: Literal[MemoryLayer.POLICY] = MemoryLayer.POLICY
    path: str
    revision_ref: str | None = None
    managed: bool = True
    metadata: dict = Field(default_factory=dict)


"""Project 层原子文档投影。"""

class ProjectItem(MemoryItem):
    layer: Literal[MemoryLayer.PROJECT] = MemoryLayer.PROJECT
    path: str
    revision_ref: str | None = None
    metadata: dict = Field(default_factory=dict)


"""User 层结构化长期记忆。"""

class UserMemoryItem(BaseModel):
    """一张有证据的原子用户卡；经验字段描述一次观察。"""

    model_config = ConfigDict(frozen=True)

    item_id: str
    layer: Literal[MemoryLayer.USER] = MemoryLayer.USER
    kind: UserCardKind
    slot_key: str | None = None
    source_type: str
    source_ref: str
    scope: MemoryScope = Field(default_factory=MemoryScope)
    title: str | None = None
    content: str
    summary: str | None = None
    priority: int = Field(default=50, ge=0, le=100)
    token_count: int = Field(default=0, ge=0)
    content_hash: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    status: UserCardStatus = UserCardStatus.ACTIVE
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_authority: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    supersedes_item_id: str | None = None
    revision: int = Field(default=1, ge=1)
    last_confirmed_at: datetime | None = None
    keywords: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    applicability: str | None = None
    action: str | None = None
    outcome: str | None = None
    limitations: str | None = None
    outcome_status: ExperienceOutcome | None = None

    @model_validator(mode="after")
    def _validate_card(self) -> "UserMemoryItem":
        if self.content_hash != content_sha256(self.content):
            raise ValueError("content_hash 与 content 不一致")
        if self.kind is UserCardKind.REUSABLE_EXPERIENCE:
            if self.slot_key is not None:
                raise ValueError("经验卡片没有事实槽位")
        elif not self.slot_key:
            raise ValueError("事实卡片必须有 slot_key")
        return self


# """Scratchpad 层单轮临时原子。"""

# class ScratchpadItem(MemoryItem):
#     layer: Literal[MemoryLayer.SCRATCHPAD] = MemoryLayer.SCRATCHPAD
#     turn_id: str
#     expires_at: datetime | None = None
