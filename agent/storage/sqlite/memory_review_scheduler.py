"""持久化成功轮次的提炼节奏，并排入固定来源的批次作业。

职责：按用户 scope 累计轮次，在渐进边界或重置/退出时创建作业。
边界：只安排来源 turn ID，不读取原文、不生成卡片。
关联：SessionStore 提交轮次时调用；MemoryReviewWorker 消费生成的作业。
"""
from __future__ import annotations

import uuid

from .codec import milliseconds
from .job_store import enqueue_job

## 递进式提取按用户 scope 跨 Session 累计，间隔逐渐增加
def _is_cadence_boundary(ordinal: int) -> bool:
    return ordinal in (1, 3, 6, 10) or (ordinal >= 15 and (ordinal - 15) % 5 == 0)


class SqliteMemoryReviewScheduler:
    def __init__(self, database, *, extractor_version: str):
        self.db = database
        self.extractor_version = extractor_version
  
    def bootstrap(self) -> None:
        """接管旧版已提交轮次与作业，不重复执行已经完成的提炼。"""
        with self.db.write() as conn:
            conn.execute(
                "UPDATE jobs SET handler_version=?,revision=revision+1 "
                "WHERE job_type='memory.extract' AND status<>'completed' "
                "AND handler_version<>?",
                (self.extractor_version, self.extractor_version),
            )
            rows = conn.execute(
                "SELECT t.turn_id,s.scope_key,t.completed_at FROM turns t "
                "JOIN sessions s ON s.session_id=t.session_id "
                "WHERE t.status='completed' ORDER BY s.scope_key,t.completed_at,t.turn_id"
            ).fetchall()
            ordinals: dict[str, int] = {}
            for turn_id, scope_key, _ in rows:
                ordinals[scope_key] = ordinals.get(scope_key, 0) + 1
                if conn.execute(
                    "SELECT 1 FROM memory_review_turns WHERE turn_id=?", (turn_id,)
                ).fetchone():
                    continue
                legacy = conn.execute(
                    "SELECT id,status FROM jobs WHERE id=? AND job_type='memory.extract'",
                    (turn_id,),
                ).fetchone()
                assignment = legacy[0] if legacy else f"legacy:{turn_id}"
                conn.execute(
                    "INSERT INTO memory_review_turns "
                    "(turn_id,scope_key,ordinal,batch_job_id,created_at) VALUES (?,?,?,?,?)",
                    (turn_id, scope_key, ordinals[scope_key], assignment, milliseconds()),
                )
                if legacy and legacy[1] != "completed":
                    conn.execute(
                        "UPDATE jobs SET handler_version=? WHERE id=?",
                        (self.extractor_version, legacy[0]),
                    )

    def record_turn(self, conn, turn) -> None:
        if not turn.completed or turn.interrupted:
            return
        if conn.execute(
            "SELECT 1 FROM memory_review_turns WHERE turn_id=?", (turn.turn_id,)
        ).fetchone():
            return
        scope_key = turn.scope.storage_key
        ordinal = conn.execute(
            "SELECT COALESCE(MAX(ordinal),0)+1 FROM memory_review_turns WHERE scope_key=?",
            (scope_key,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO memory_review_turns "
            "(turn_id,scope_key,ordinal,batch_job_id,created_at) VALUES (?,?,?,?,?)",
            (turn.turn_id, scope_key, ordinal, None, milliseconds()),
        )
        if _is_cadence_boundary(ordinal):
            self.flush_scope(conn, scope_key, trigger_type="cadence")

    def flush_scope(self, conn, scope_key: str, *, trigger_type: str = "flush"):
        rows = conn.execute(
            "SELECT r.turn_id,t.session_id FROM memory_review_turns r "
            "JOIN turns t ON t.turn_id=r.turn_id "
            "WHERE r.scope_key=? AND r.batch_job_id IS NULL ORDER BY r.ordinal",
            (scope_key,),
        ).fetchall()
        if not rows:
            return None
        turn_ids = tuple(row[0] for row in rows)
        last_turn, last_session = rows[-1]
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL,
            "memory.extract:" + scope_key + ":" + ",".join(turn_ids)))
        job = enqueue_job(
            conn,
            job_type="memory.extract",
            handler_version=self.extractor_version,
            session_id=last_session,
            turn_id=last_turn,
            job_id=identity,
            dedupe_key=f"memory.extract:{self.extractor_version}:{identity}",
            input={"scope_key": scope_key, "turn_ids": turn_ids, "trigger_type": trigger_type},
        )
        conn.executemany(
            "UPDATE memory_review_turns SET batch_job_id=? WHERE turn_id=?",
            ((job.id, turn_id) for turn_id in turn_ids),
        )
        return job

    def flush_all(self) -> int:
        with self.db.write() as conn:
            scopes = conn.execute(
                "SELECT DISTINCT scope_key FROM memory_review_turns "
                "WHERE batch_job_id IS NULL ORDER BY scope_key"
            ).fetchall()
            return sum(
                self.flush_scope(conn, scope_key, trigger_type="shutdown") is not None
                for (scope_key,) in scopes
            )
