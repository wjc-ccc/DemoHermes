"""
Agent/memory 包

精选记忆（Hot: USER/GROUP/MEMORY.md）、Daily、SQLite 情景记忆与外部 Provider，
由 MemoryManager 统一编排（7.23 方案 §2 Seam A）。对上层只暴露类型与 Manager。
"""
from .manager import MemoryManager
from .snapshot import DocumentRevision, MemoryContextSnapshot
from .types import (
    BuiltinMemoryMutation,
    MemoryDiagnostic,
    MemoryEntry,
    MemoryMutationResult,
    MemoryProvenance,
    MemoryScope,
)

__all__ = [
    "BuiltinMemoryMutation",
    "DocumentRevision",
    "MemoryContextSnapshot",
    "MemoryDiagnostic",
    "MemoryEntry",
    "MemoryManager",
    "MemoryMutationResult",
    "MemoryProvenance",
    "MemoryScope",
]
