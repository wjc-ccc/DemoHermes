"""SQLite Store 对调用方暴露的结果；Job 统一使用毫秒时间。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import time
import uuid
from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class ArchivedMessage:
    session_id: str
    message_id: str
    content_text: str
    created_at: float

@dataclass(frozen=True)
class Job:
    id: str
    job_type: str
    handler_version: str
    session_id: str | None
    turn_id: str | None
    dedupe_key: str
    input_hash: str
    input: dict[str, Any]
    status: str
    revision: int
    attempt_count: int
    created_at: int
    available_at: int
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: int | None = None
    last_error: str | None = None
    result: dict[str, Any] | None = None


@dataclass(frozen=True)
class MemoryReviewCommitResult:
    job_id: str
    trace_id: str
    user_item_count: int
    idempotent: bool = False


@dataclass(frozen=True)
class MemoryMutationReceipt:
    item_id: str
    revision: int
    content_hash: str


@dataclass(frozen=True)
class TurnCommitResult:
    turn_id: str
    persisted: bool
    mutations: tuple[MemoryMutationReceipt, ...] = ()


class AuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    audit_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    at: float = Field(default_factory=time.time)
    actor: str
    op: str
    target: str = "memory"
    content_preview: str = ""
    turn_id: str | None = None
    reason: str = ""
    admitted: bool = True
    scope_key: str = ""
