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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        return self


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


class MemoryDiagnostic(BaseModel):
    """非致命降级：记录发生了什么，不阻断主流程。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str = ""
