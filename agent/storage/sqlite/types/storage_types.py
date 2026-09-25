"""SQLite Store 对调用方暴露的结果；Job 统一使用毫秒时间。"""
from dataclasses import dataclass, field
from typing import Any

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
