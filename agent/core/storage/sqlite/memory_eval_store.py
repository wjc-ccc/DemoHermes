"""Compatibility import for the previous memory-only evaluation store name."""
from .eval_store import DatasetVersionConflictError, SqliteEvalStore
SqliteMemoryEvalStore = SqliteEvalStore
