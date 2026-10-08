"""五层 Memory 领域模型、冻结快照与提取审查；不包含 SQLite 实现。"""
# 职责：提供 Memory 领域的集中导入入口。
# 边界：本包不直接组装 Gateway，也不实现数据库表和模型工具。
# 关联：生产装配在 main.py；SQLite Adapter 与 memory_tools.py 从这里导入领域对象。

from .markdown_book import (
    MarkdownBook,
    MarkdownBookReadOnlyError,
    MarkdownBookSnapshot,
    MarkdownRevisionConflictError,
    estimate_markdown_tokens,
)
from .markdown_editor import (
    ManualRead,
    MemoryManualContentError,
    MemoryManualEditor,
    MemoryManualFileInvalidError,
    MemoryManualScopeError,
)
from .memory_context_loader import MemoryContextLoader
from .memory_extractor import MemoryExtractor, MemoryExtractionError
from .memory_item import MemoryItem, content_sha256
from .memory_layer import (
    MemoryLayer,
    authority_rank,
    item_types_for,
    validate_item_type,
)
from .memory_review_worker import MemoryReviewWorker
from .memory_scope import MemoryScope, build_memory_scope, ensure_scope_dir
from .memory_snapshot import DocumentRevision, MemoryDiagnostic, MemorySnapshot
from .memory_trace import (
    MemoryAiTraceFields,
    MemoryItemTrace,
    MemoryRuntimeTraceFields,
    MemoryTrace,
    MemoryTraceDecision,
    MemoryTraceOperation,
)
from .memory_validator import MemoryValidator
from .memory_item import PolicyItem, ProjectItem, UserMemoryItem
from .types.card_types import UserCardKind, UserCardStatus, ExperienceOutcome
from .memory_batch_reviewer import MemoryReviewResult, MemoryBatchReviewer


__all__ = [
    "MarkdownBook",
    "MarkdownBookReadOnlyError",
    "MarkdownBookSnapshot",
    "MarkdownRevisionConflictError",
    "ManualRead",
    "MemoryManualContentError",
    "MemoryManualEditor",
    "MemoryManualFileInvalidError",
    "MemoryManualScopeError",
    "MemoryContextLoader",
    "MemoryDiagnostic",
    "MemoryItem",
    "UserCardKind",
    "UserCardStatus",
    "ExperienceOutcome",
    "MemoryLayer",
    "MemoryExtractor",
    "MemoryExtractionError",
    "MemoryAiTraceFields",
    "MemoryItemTrace",
    "MemoryRuntimeTraceFields",
    "MemoryReviewResult",
    "MemoryScope",
    "MemorySnapshot",
    "MemoryTrace",
    "MemoryTraceDecision",
    "MemoryTraceOperation",
    "MemoryValidator",
    "DocumentRevision",
    "PolicyItem",
    "ProjectItem",
    # "ScratchpadItem",
    "MemoryBatchReviewer",
    "MemoryReviewWorker",
    "UserMemoryItem",
    "authority_rank",
    "build_memory_scope",
    "content_sha256",
    "ensure_scope_dir",
    "estimate_markdown_tokens",
    "item_types_for",
    "validate_item_type",
]
