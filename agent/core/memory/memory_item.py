"""跨来源统一的轻量 MemoryItem 投影。"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Mapping, Literal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .memory_layer import MemoryLayer, validate_item_type
from .memory_scope import MemoryScope
from .session_narrative import EvidenceRef


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


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

class UserMemoryKind(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class UserMemoryStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DELETED = "deleted"


class UserMemoryItem(MemoryItem):
    layer: Literal[MemoryLayer.USER] = MemoryLayer.USER
    memory_kind: UserMemoryKind | None = None
    normalized_key: str
    status: UserMemoryStatus = UserMemoryStatus.PROPOSED
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_authority: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    supersedes_item_id: str | None = None
    revision: int = Field(default=1, ge=1)
    last_confirmed_at: datetime | None = None
    keywords: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()



"""Scratchpad 层单轮临时原子。"""

class ScratchpadItem(MemoryItem):
    layer: Literal[MemoryLayer.SCRATCHPAD] = MemoryLayer.SCRATCHPAD
    turn_id: str
    expires_at: datetime | None = None
