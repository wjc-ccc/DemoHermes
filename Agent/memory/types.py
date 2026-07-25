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

from pydantic import BaseModel, ConfigDict, Field


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


class MemoryDiagnostic(BaseModel):
    """非致命降级：记录发生了什么，不阻断主流程。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str = ""
