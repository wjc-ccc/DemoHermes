"""Markdown 读取结果；有效快照与实际磁盘哈希有不同用途。"""
from dataclasses import dataclass
from agent.types.memory.snapshot import MarkdownBookSnapshot, MemoryDiagnostic


@dataclass(frozen=True)
class DocumentRead:
    snapshot: MarkdownBookSnapshot
    diagnostics: tuple[MemoryDiagnostic, ...]
    disk_hash: str
    revision: int | None = None


class MarkdownRevisionConflictError(Exception):
    error_code = "markdown_revision_conflict"

class MemoryManualError(ValueError):
    error_code = "memory_manual_error"

class MemoryManualScopeError(MemoryManualError):
    error_code = "memory_manual_scope_denied"

class MemoryManualContentError(MemoryManualError):
    error_code = "memory_manual_content_invalid"

class MemoryManualFileInvalidError(MemoryManualError):
    error_code = "memory_manual_file_invalid"
