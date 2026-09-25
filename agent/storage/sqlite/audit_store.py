"""Append-only management audit."""
from .codec import milliseconds

class SqliteAuditStore:
    def __init__(self, db):
        self.db = db

    def append(self, entry):
        entry=entry.model_copy(update={"content_preview":entry.content_preview[:500]})
        with self.db.write() as conn:
            conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?)",
                (entry.audit_id,entry.actor,entry.op,entry.target,entry.turn_id,
                 entry.scope_key,milliseconds(entry.at),entry.model_dump_json()))
        return entry.audit_id

    def list_recent(self, n=20):
        from ...evolution.audit import EvolutionAuditEntry
        with self.db.read() as conn:
            rows=conn.execute("SELECT payload_json FROM audit_events ORDER BY created_at DESC,id DESC LIMIT ?",
                              (max(1,min(n,500)),)).fetchall()
        return [EvolutionAuditEntry.model_validate_json(r[0]) for r in rows]
