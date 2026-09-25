"""Worker dependencies."""
from __future__ import annotations
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..session_memory_reviewer import MemoryReviewResult

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


