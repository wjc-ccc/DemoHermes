"""Memory 领域所需的持久化 Protocol。"""
# 职责：描述卡片和 Trace 存储的最小接口。
# 边界：仅为依赖约定，不决定 SQLite 表结构、事务和冲突规则。
# 关联：memory_store.py、trace_store.py 是对应的持久化实现。
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..memory_trace import MemoryTrace
from ..memory_item import UserMemoryItem


class MemoryStore(Protocol):
    def put(self, item: UserMemoryItem) -> UserMemoryItem: ...

    def get(self, item_id: str) -> UserMemoryItem | None: ...


class MemoryTraceStore(Protocol):
    def put(self, trace: MemoryTrace) -> MemoryTrace: ...

    def get(self, trace_id: str) -> MemoryTrace | None: ...

    def list_recent(
        self,
        *,
        session_id: str | None = None,
        item_id: str | None = None,
        limit: int = 50,
    ) -> tuple[MemoryTrace, ...]: ...

    def delete_before(self, cutoff: datetime) -> int: ...
