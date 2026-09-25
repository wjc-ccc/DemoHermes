"""Memory 领域所需的持久化 Protocol。"""
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
