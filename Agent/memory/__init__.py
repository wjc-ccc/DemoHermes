"""
Agent/memory 包

精选记忆（Hot: USER/GROUP/MEMORY.md）、Daily、SQLite 情景记忆与外部 Provider，
由 MemoryManager 统一编排（7.23 方案 §2 Seam A）。对上层只暴露类型与 Manager。
"""
from .manager import MemoryManager
from .scope import build_memory_scope
from .snapshot import DocumentRevision, MemorySnapshot
from .types import (
    BuiltinMemoryRequest,
    MemoryReadRequest,
    DailyEntryRef,
    MemoryDiagnostic,
    MemoryDocumentView,
    MemoryEntry,
    MemoryHit,
    BuiltinMemoryResult,
    MemoryProvenance,
    MemoryScope,
    MemorySearchRequest,
)

__all__ = [
    "BuiltinMemoryRequest",
    "MemoryReadRequest",
    "DailyEntryRef",
    "DocumentRevision",
    "MemorySnapshot",
    "MemoryDiagnostic",
    "MemoryDocumentView",
    "MemoryEntry",
    "MemoryHit",
    "MemoryManager",
    "BuiltinMemoryResult",
    "MemoryProvenance",
    "MemoryScope",
    "MemorySearchRequest",
    "build_memory_scope",
]
