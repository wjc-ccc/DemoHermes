"""
Memory 核心类型契约

只放跨模块共用的不可变类型。Provider 只有 recall/sync，不在此定义 CRUD 类型。
"""
from __future__ import annotations

import datetime
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.types import Session

_DAILY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_daily_date(value: str) -> None:
    """daily date 必须是严格 YYYY-MM-DD 且为真实日历日期。"""
    if not _DAILY_DATE_RE.match(value):
        raise ValueError(f"daily date 必须是严格 YYYY-MM-DD: {value!r}")
    datetime.date.fromisoformat(value)


class MemoryScope(BaseModel):
    """记忆隔离边界：personal 按 channel+user，group 按 channel+chat。"""

    model_config = ConfigDict(frozen=True)

    kind: Literal["personal", "group"]
    key: str


class MemoryProvenance(BaseModel):
    """一条记忆从哪来；reviewer 写入必须带 turn_id 供幂等去重。"""

    model_config = ConfigDict(frozen=True)

    source: Literal["user_request", "reviewer", "tool"]
    turn_id: str | None = None


MemoryTarget = Literal["profile", "memory", "daily"]
MemoryAction = Literal["add", "replace", "remove"]


class BuiltinMemoryRequest(BaseModel):
    """内置文档（USER/GROUP/MEMORY/daily）的一次写入请求。"""

    scope: MemoryScope
    target: MemoryTarget
    action: MemoryAction
    content: str | None = None
    entry_id: str | None = None
    date: str | None = None
    expected_revision: str | None = None
    provenance: MemoryProvenance

    @model_validator(mode="after")
    def _check_field_combination(self) -> "BuiltinMemoryRequest":
        if self.action == "add":
            if not self.content:
                raise ValueError("add 必须提供 content")
            if self.entry_id is not None:
                raise ValueError("add 不允许指定 entry_id（由 Store 生成）")
        elif self.action == "replace":
            if not self.entry_id or not self.content:
                raise ValueError("replace 必须提供 entry_id 和 content")
        elif self.action == "remove":
            if not self.entry_id:
                raise ValueError("remove 必须提供 entry_id")
            if self.content is not None:
                raise ValueError("remove 不允许携带 content")
        if self.target in ("profile", "memory") and self.date is not None:
            raise ValueError("profile/memory 不得携带 date")
        if self.target == "daily" and self.date is not None:
            _check_daily_date(self.date)
        return self


class BuiltinMemoryResult(BaseModel):
    """BuiltinMemoryRequest 的写入结果。"""

    ok: bool
    entry_id: str | None = None
    revision: str | None = None
    compacted: bool = False
    backup_id: str | None = None
    conflict: bool = False
    error_code: str | None = None
    message: str = ""


class MemoryEntry(BaseModel):
    """Markdown 中以 <!-- memory-entry:ID --> 围栏标记的一条记忆。"""

    model_config = ConfigDict(frozen=True)

    entry_id: str
    content: str


class MemoryDocumentView(BaseModel):
    """一份内置文档的可读视图（read 的返回）。"""

    model_config = ConfigDict(frozen=True)

    target: MemoryTarget
    revision: str
    entries: tuple[MemoryEntry, ...]
    rendered: str


class MemoryReadRequest(BaseModel):
    """读取内置文档：profile/memory 整份，或指定 date 的 daily。"""

    scope: MemoryScope
    target: MemoryTarget
    date: str | None = None

    @model_validator(mode="after")
    def _check_date(self) -> "MemoryReadRequest":
        if self.target in ("profile", "memory") and self.date is not None:
            raise ValueError("profile/memory 读取不得携带 date")
        if self.target == "daily" and self.date is not None:
            _check_daily_date(self.date)
        return self


class MemoryDiagnostic(BaseModel):
    """非致命降级：记录发生了什么，不阻断主流程。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str = ""


MemorySearchSource = Literal["daily", "session"]


class DailyEntryRef(BaseModel):
    """一条 daily 命中的稳定引用：日期 + entry_id。"""

    model_config = ConfigDict(frozen=True)

    source: Literal["daily"] = "daily"
    date: str
    entry_id: str


class SessionMessageRef(BaseModel):
    """一条 session 消息命中的稳定引用。"""

    model_config = ConfigDict(frozen=True)

    source: Literal["session"] = "session"
    session_id: str
    message_id: str


MemoryHitRef = Annotated[
    DailyEntryRef | SessionMessageRef,
    Field(discriminator="source"),
]


class MemorySearchRequest(BaseModel):
    """渐进检索请求；scope 由运行上下文注入。"""

    scope: MemoryScope
    query: str
    sources: tuple[MemorySearchSource, ...] = ("daily", "session")
    limit: int = Field(default=8, ge=1, le=50)
    snippet_max_chars: int = Field(default=500, ge=50, le=2000)

    @model_validator(mode="after")
    def _check_query_and_sources(self) -> "MemorySearchRequest":
        if not self.query.strip():
            raise ValueError("query 不能为空")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources 不允许重复")
        return self


class MemoryHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: MemorySearchSource
    score: float
    snippet: str
    occurred_at: float | None = None
    ref: MemoryHitRef


class TurnRecord(BaseModel):
    """已完成（或中断）turn 的不可变事实信封；供 Memory / Evolution 落库与复盘。"""

    model_config = ConfigDict(frozen=True)

    turn_id: str
    session_id: str
    scope: MemoryScope
    user_message_id: str
    assistant_message_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    started_at: float
    finished_at: float
    completed: bool
    interrupted: bool
    error: str | None


class SessionReset(BaseModel):
    """close_and_reset 的原子产物：关闭的旧 Session 与新 Session。"""

    closed_session: Session | None
    new_session: Session


class ProviderRecallRequest(BaseModel):
    """请求外部 Provider 针对当前 query 返回相关上下文。"""

    scope: MemoryScope
    query: str
    session_id: str
    deadline: float | None = None


class ProviderContribution(BaseModel):
    """Provider recall 的贡献：name + 文本。"""

    name: str
    text: str = ""
