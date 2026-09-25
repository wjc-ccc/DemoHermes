"""Memory observations stored separately from Precision's original turn payload."""
from __future__ import annotations
import json
from datetime import datetime
from ...core.memory.memory_trace import MemoryTrace
from .database import Database
from .codec import milliseconds


def put_trace(conn, trace: MemoryTrace) -> MemoryTrace:
    conn.execute("INSERT INTO traces VALUES (?,?,?,?,?,?,?,?)",
        (trace.trace_id, "memory", trace.operation.value, trace.session_id, trace.turn_id,
         trace.job_id, milliseconds(trace.created_at), trace.model_dump_json(exclude={"items"})))
    for item in trace.items:
        conn.execute("INSERT INTO trace_items VALUES (?,?,?,?,?,?,?,?)",
            (item.trace_item_id,trace.trace_id,item.item_id,item.decision.value,
             milliseconds(item.created_at),item.model_dump_json(exclude={"ai","runtime"}),
             item.ai.model_dump_json(),item.runtime.model_dump_json()))
    return trace


class SqliteTraceStore:
    def __init__(self, db: Database):
        self.db = db

    def put(self, trace: MemoryTrace):
        with self.db.write() as conn:
            return put_trace(conn,trace)

    @staticmethod
    def _get_with_conn(conn, trace_id):
        row=conn.execute("SELECT payload_json FROM traces WHERE id=? AND module='memory'",(trace_id,)).fetchone()
        if not row:
            return None
        data=json.loads(row[0])
        data["items"]=[dict(json.loads(r[0]), ai=json.loads(r[1]), runtime=json.loads(r[2]))
            for r in conn.execute("SELECT payload_json,ai_json,runtime_json FROM trace_items "
                                  "WHERE trace_id=? ORDER BY rowid",(trace_id,))]
        return MemoryTrace.model_validate(data)

    def get(self, trace_id: str):
        with self.db.read() as conn:
            conn.execute("BEGIN")
            return self._get_with_conn(conn,trace_id)

    def list_recent(self, *, session_id=None, item_id=None, limit=50, offset=0):
        where=["t.module='memory'"]
        args=[]
        if session_id is not None:
            where.append("t.session_id=?")
            args.append(session_id)
        if item_id is not None:
            where.append("EXISTS(SELECT 1 FROM trace_items i WHERE i.trace_id=t.id AND i.item_id=?)")
            args.append(item_id)
        with self.db.read() as conn:
            conn.execute("BEGIN")
            rows=conn.execute("SELECT t.id FROM traces t WHERE "+" AND ".join(where)+
                " ORDER BY t.created_at DESC,t.id DESC LIMIT ? OFFSET ?",
                (*args,max(1,min(limit,500)),max(0,offset))).fetchall()
            return tuple(self._get_with_conn(conn,r[0]) for r in rows)

    def delete_before(self, cutoff: datetime):
        with self.db.write() as conn:
            return conn.execute("DELETE FROM traces WHERE module='memory' AND created_at<?",
                                (milliseconds(cutoff),)).rowcount
