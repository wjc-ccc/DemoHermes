"""后台提炼 Worker 的队列与提交协议。

职责：声明 Worker 取得来源轮次、Session 和提交结果所需的接口。
边界：只约束依赖形状，不实现租约、重试或 SQLite 事务。
关联：MemoryReviewWorker 使用这些协议，RuntimeStorage 提供实现。
"""
from __future__ import annotations
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory_batch_reviewer import MemoryReviewResult

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


