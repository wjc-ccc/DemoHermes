"""EvolutionAuditLog — 进化审计表（8.04 Phase 5.3）。

记录谁在何时对哪条 entry 做了何种 delta；SQLite 单文件，旁路观测。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class EvolutionAuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    audit_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    at: float = Field(default_factory=time.time)
    actor: str
    op: str
    target: str = "memory"
    content_preview: str = ""
    turn_id: str | None = None
    reason: str = ""
    admitted: bool = True
    scope_key: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_audit (
  audit_id TEXT PRIMARY KEY,
  at REAL NOT NULL,
  actor TEXT NOT NULL,
  op TEXT NOT NULL,
  target TEXT NOT NULL,
  content_preview TEXT NOT NULL,
  turn_id TEXT,
  reason TEXT NOT NULL,
  admitted INTEGER NOT NULL,
  scope_key TEXT NOT NULL
);
"""


class EvolutionAuditLog:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def append(self, entry: EvolutionAuditEntry) -> str:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evolution_audit("
                "audit_id, at, actor, op, target, content_preview, "
                "turn_id, reason, admitted, scope_key"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.audit_id,
                    entry.at,
                    entry.actor,
                    entry.op,
                    entry.target,
                    entry.content_preview[:500],
                    entry.turn_id,
                    entry.reason,
                    1 if entry.admitted else 0,
                    entry.scope_key,
                ),
            )
        return entry.audit_id

    def list_recent(self, n: int = 20) -> list[EvolutionAuditEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evolution_audit ORDER BY at DESC LIMIT ?",
                (max(1, n),),
            ).fetchall()
        return [
            EvolutionAuditEntry(
                audit_id=r["audit_id"],
                at=r["at"],
                actor=r["actor"],
                op=r["op"],
                target=r["target"],
                content_preview=r["content_preview"],
                turn_id=r["turn_id"],
                reason=r["reason"],
                admitted=bool(r["admitted"]),
                scope_key=r["scope_key"],
            )
            for r in rows
        ]
