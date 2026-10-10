"""Memory 领域所需的持久化 Protocol。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from agent.types.memory.trace import MemoryTrace
from agent.types.memory.items import UserMemoryItem
from agent.types.memory.snapshot import MarkdownBookSnapshot
from agent.types.memory.scope import MemoryScope
from agent.types.memory.extraction import MemoryReviewResult
from agent.types.memory.snapshot import MemorySnapshot
from agent.types.tools import ToolDefinition


class MemorySnapshotLoader(Protocol):
    def freeze(self, scope: MemoryScope, *, session_id: str, query: str = "") -> MemorySnapshot: ...


class MemoryWorker(Protocol):
    def start(self) -> None: ...
    def stop(self, timeout: float | None = None) -> None: ...
    def notify(self, *_args, **_kwargs) -> None: ...
    def process_pending(self) -> int: ...


@dataclass(frozen=True)
class MemoryBuildResult:
    context: MemorySnapshotLoader
    tools: tuple[ToolDefinition, ...]
    reviewer: MemoryWorker


# 职责：描述卡片和 Trace 存储的最小接口。
# 边界：仅为依赖约定，不决定 SQLite 表结构、事务和冲突规则。
# 关联：memory_store.py、trace_store.py 是对应的持久化实现。


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


"""后台提炼 Worker 的队列与提交协议。

职责：声明 Worker 取得来源轮次、Session 和提交结果所需的接口。
边界：只约束依赖形状，不实现租约、重试或 SQLite 事务。
关联：MemoryReviewWorker 使用这些协议，RuntimeStorage 提供实现。
"""

class MemoryReviewQueue(Protocol):
    jobs: Any
    executions: Any
    def get_session(self, session_id: str) -> Any: ...

class MemoryReviewCommitter(Protocol):
    def commit(
        self,
        job: Any,
        result: MemoryReviewResult,
        *,
        extractor_version: str,
        now: int | None = None,
    ) -> Any: ...


class DocumentRegistry(Protocol):
    def register(self, snapshot: MarkdownBookSnapshot) -> str: ...


class ExperienceRetriever(Protocol):
    def recall(self, scope: MemoryScope, query: str, *, limit: int = 3, token_budget: int = 600): ...
