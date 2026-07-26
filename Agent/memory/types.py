"""
Memory 核心类型契约（7.23 方案 §3）

只放跨模块共用的不可变类型：
    - MemoryScope            : personal/group 逻辑键（不直接用作目录名）
    - MemoryProvenance       : 记忆来源（哪个 turn / 哪个入口写入）
    - BuiltinMemoryMutation  : 内置精选记忆的判别写入（profile/memory/daily）
    - MemoryMutationResult   : 写入结果（ok/entry_id/revision/conflict/error_code）
    - MemoryEntry            : 带稳定 entry_id 的单条记忆
    - MemoryDiagnostic       : 非致命降级信息（随快照/结果返回）

判别校验（add 必须无 entry_id 等）在 Slice 2 随 CRUD 测试补齐。
"""
from __future__ import annotations

import datetime
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.types import Session

_DAILY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_daily_date(value: str) -> None:
    """§3.2：daily date 必须是严格 YYYY-MM-DD 且为真实日历日期（也保证路径安全）。"""
    if not _DAILY_DATE_RE.match(value):
        raise ValueError(f"daily date 必须是严格 YYYY-MM-DD: {value!r}")
    datetime.date.fromisoformat(value)  # 非法日历日期（如 2026-02-30）在此抛 ValueError


class MemoryScope(BaseModel):
    """记忆隔离边界：personal 按 channel+user，group 按 channel+chat。"""

    model_config = ConfigDict(frozen=True)

    kind: Literal["personal", "group"]
    key: str  # 规范化逻辑键，如 "cli:user:u1"；不直接拼进路径


class MemoryProvenance(BaseModel):
    """一条记忆从哪来；reviewer 写入必须带 turn_id 供幂等去重。"""

    model_config = ConfigDict(frozen=True)

    source: Literal["user_request", "reviewer", "tool", "provider"]
    turn_id: str | None = None


MemoryTarget = Literal["profile", "memory", "daily"]
MemoryAction = Literal["add", "replace", "remove"]


class BuiltinMemoryMutation(BaseModel):
    """内置文档（USER/GROUP/MEMORY/daily）的一次写入请求。"""

    backend: Literal["builtin"] = "builtin"
    scope: MemoryScope
    target: MemoryTarget
    action: MemoryAction
    content: str | None = None
    entry_id: str | None = None
    date: str | None = None
    expected_revision: str | None = None
    provenance: MemoryProvenance

    @model_validator(mode="after")
    def _check_field_combination(self) -> "BuiltinMemoryMutation":
        """§3.2 字段约束：非法组合在模型层直接拒绝，不进 Store。"""
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


class ExternalMemoryMutation(BaseModel):
    """外部 Provider 的一次记忆写入请求（§3.2）。"""

    backend: Literal["external"] = "external"
    scope: MemoryScope
    action: MemoryAction
    memory_id: str | None = None
    content: str | None = None
    expected_revision: str | None = None
    provenance: MemoryProvenance

    @model_validator(mode="after")
    def _check_field_combination(self) -> "ExternalMemoryMutation":
        """§3.2 字段约束：External add/replace/remove 的 memory_id/content 组合。"""
        if self.action == "add":
            if not self.content:
                raise ValueError("add 必须提供 content")
            if self.memory_id is not None:
                raise ValueError("add 不允许指定 memory_id（由 Provider 生成）")
        elif self.action == "replace":
            if not self.memory_id or not self.content:
                raise ValueError("replace 必须提供 memory_id 和 content")
        elif self.action == "remove":
            if not self.memory_id:
                raise ValueError("remove 必须提供 memory_id")
            if self.content is not None:
                raise ValueError("remove 不允许携带 content")
        return self


# §3.2：精选记忆写入判别联合--backend 决定走 builtin 文档还是 external Provider
MemoryMutation = Annotated[
    BuiltinMemoryMutation | ExternalMemoryMutation,
    Field(discriminator="backend"),
]


class MemoryMutationResult(BaseModel):
    ok: bool
    entry_id: str | None = None
    memory_id: str | None = None
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


class BuiltinMemoryReadRequest(BaseModel):
    """读取内置文档：profile/memory 整份，或指定 date 的 daily。"""

    backend: Literal["builtin"] = "builtin"
    scope: MemoryScope
    target: MemoryTarget
    date: str | None = None

    @model_validator(mode="after")
    def _check_date(self) -> "BuiltinMemoryReadRequest":
        if self.target in ("profile", "memory") and self.date is not None:
            raise ValueError("profile/memory 读取不得携带 date")
        if self.target == "daily" and self.date is not None:
            _check_daily_date(self.date)
        return self


class ExternalMemoryReadRequest(BaseModel):
    """读取外部 Provider 的一条记忆（§3.2）。"""

    backend: Literal["external"] = "external"
    scope: MemoryScope
    memory_id: str


# §3.2：读取请求判别联合--backend 决定走 builtin 文档还是 external Provider
MemoryReadRequest = Annotated[
    BuiltinMemoryReadRequest | ExternalMemoryReadRequest,
    Field(discriminator="backend"),
]


class ProviderDocument(BaseModel):
    """外部 Provider 一条记忆的可读视图（external read 的返回）。"""

    model_config = ConfigDict(frozen=True)

    memory_id: str
    content: str
    revision: str | None = None


MemoryReadResult = MemoryDocumentView | ProviderDocument


class ProviderMutation(BaseModel):
    """转发给外部 Provider 的写入请求（由 ExternalMemoryMutation 派生，§2 Seam D）。"""

    action: MemoryAction
    memory_id: str | None = None
    content: str | None = None
    expected_revision: str | None = None


class ProviderMutationResult(BaseModel):
    """Provider 写入结果；Manager 据此装配 MemoryMutationResult。"""

    ok: bool
    memory_id: str | None = None
    revision: str | None = None
    error_code: str | None = None
    message: str = ""


class ProviderReadRequest(BaseModel):
    """转发给外部 Provider 的读取请求（§2 Seam D）。"""

    scope: MemoryScope
    memory_id: str


class MemoryDiagnostic(BaseModel):
    """非致命降级：记录发生了什么，不阻断主流程。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str = ""


# ---- §3.6 检索类型（Slice 6 先落地 daily，session/provider ref 在 Slice 11/12 扩展为联合）----

MemorySearchSource = Literal["daily", "session", "provider"]


class DailyEntryRef(BaseModel):
    """一条 daily 命中的稳定引用：日期 + entry_id；不含路径。"""

    model_config = ConfigDict(frozen=True)

    source: Literal["daily"] = "daily"
    date: str
    entry_id: str


class SessionMessageRef(BaseModel):
    """一条 session 消息命中的稳定引用：session_id + message_id；不含 SQL。"""

    model_config = ConfigDict(frozen=True)

    source: Literal["session"] = "session"
    session_id: str
    message_id: str


# §3.6：MemoryHitRef 是按 source 判别的联合（provider ref 在 Slice 12 加入）
MemoryHitRef = Annotated[
    DailyEntryRef | SessionMessageRef,
    Field(discriminator="source"),
]


class MemorySearchRequest(BaseModel):
    """渐进检索请求；scope 永远由运行上下文注入，模型不能伪造。"""

    scope: MemoryScope
    query: str
    sources: tuple[MemorySearchSource, ...] = ("daily", "session", "provider")
    limit: int = Field(default=8, ge=1, le=50)
    snippet_max_chars: int = Field(default=500, ge=50, le=2000)

    @model_validator(mode="after")
    def _check_query_and_sources(self) -> "MemorySearchRequest":
        """§3.6：空白 query、重复 source 在进入 adapter 前拒绝（未知 source 由 Literal 拒绝）。"""
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


# ---- §3.3 Turn 事实信封（turn 持久化 / Provider sync / review job 的幂等键）----


class TurnEnvelope(BaseModel):
    """一个已完成（或中断）turn 的不可变事实摘要；turn_id 由 Loop 生成。"""

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


class ResetResult(BaseModel):
    """close_and_reset 的原子转换结果；事务成功后 Gateway 才对 closed_session 发结束 hook。"""

    closed_session: Session | None
    new_session: Session


# ---- §2 Seam D / §7：外部 Provider 请求与贡献类型 ----


class ProviderRecallRequest(BaseModel):
    """请求外部 Provider 针对当前 query 返回相关上下文（§2 Seam D）。"""

    scope: MemoryScope
    query: str
    session_id: str
    deadline: float | None = None  # Manager 计算的截止时刻；adapter 应据此设 HTTP/SDK timeout（§7.2）


class ProviderContribution(BaseModel):
    """Provider recall 的贡献：name + 文本（Manager 负责清洗围栏并附 provenance 标记）。"""

    name: str
    text: str = ""
