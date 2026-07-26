"""SqliteSessionStore — SessionStore seam 的 SQLite adapter（7.23 方案 §2 Seam B / §6）。

与 DictSessionStore 同一 seam；额外提供：
- 跨进程重启的持久化恢复（session_slots → sessions → messages 按 seq 组装）；
- commit_turn 单事务写入 session header + 本 turn messages + review outbox；
- 已完成 Message 首次 INSERT 后重复提交相同 payload 幂等成功（INSERT OR IGNORE）。

sessions.payload_json 保存 Session 完整字段但排除 messages/tool_calls；
恢复时只从子表按 seq 组装 messages（tool_calls 恢复在 Slice 10 落地），
避免两个事实源（§6.2 / ADR-6）。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from ...core.types import Message, Session, ToolCall
from ..types import MemoryScope, ResetResult, TurnEnvelope
from .db import connect
from .migrations import migrate

logger = logging.getLogger(__name__)


class AuditConflictError(Exception):
    """§6.3/ADR-12：已完成 Message/ToolCall 以不同 payload 重提——暴露冲突，禁止覆盖。"""

    error_code = "audit_conflict"


class SessionIntegrityError(Exception):
    """§6.3：slot / session / scope 三者不一致——拒绝跨 scope 恢复。"""

    error_code = "session_integrity_error"


class SqliteSessionStore:
    """SQLite 会话存储：单进程多线程经连接工厂各取连接（§6.1）。"""

    def __init__(self, db_path: str | Path, *, id_factory=None) -> None:
        self._db_path = Path(db_path)
        # 外部 seam（§10.1-8 允许 Fake）：session_id 生成；默认 uuid4
        self._id_factory = id_factory
        conn = connect(self._db_path)
        try:
            migrate(conn)
            conn.commit()
        finally:
            conn.close()

    def _new_session(self, session_key: str, channel: str, author_id: str) -> Session:
        session_id = (
            self._id_factory() if self._id_factory is not None else str(uuid.uuid4())
        )
        return Session(
            session_id=session_id,
            session_key=session_key,
            channel=channel,
            author_id=author_id,
        )

    # ---- 上下文寻找：session_key → Session ----
    def find(self, session_key: str) -> Session | None:
        """按 session_key 找 active session；找不到返回 None（不自动创建）。"""
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT active_session_id FROM session_slots WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if row is None:
                return None
            return self._load_session(conn, row[0])
        finally:
            conn.close()

    def get_or_create(
        self,
        session_key: str,
        *,
        scope: MemoryScope,
        channel: str = "",
        author_id: str = "",
    ) -> Session:
        """找 active session；没有则在同一事务内新建 session + 槽位。"""
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT active_session_id, scope_kind, scope_key FROM session_slots "
                "WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if row is not None:
                if (row[1], row[2]) != (scope.kind, scope.key):
                    # §6.3：slot/session/scope 三者不一致 → 完整性错误，不跨 scope 恢复
                    raise SessionIntegrityError(
                        f"session_key={session_key} 的 slot scope "
                        f"({row[1]}:{row[2]}) 与请求 scope ({scope.kind}:{scope.key}) 不一致"
                    )
                return self._load_session(conn, row[0])
            session = self._new_session(session_key, channel, author_id)
            with conn:  # 短事务：session 与槽位原子创建
                self._upsert_session(conn, session, scope)
                conn.execute(
                    "INSERT INTO session_slots "
                    "(session_key, active_session_id, scope_kind, scope_key, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_key, session.session_id, scope.kind, scope.key, time.time()),
                )
            logger.info("新建会话 key=%s id=%s", session_key, session.session_id)
            return session
        finally:
            conn.close()

    # ---- turn 持久化：事实与 review outbox 同事务（§6.3 / ADR-11）----
    def commit_turn(
        self, session: Session, turn: TurnEnvelope, *, scope: MemoryScope
    ) -> None:
        """原子保存 Session 增量，并以 turn_id 写入 review job（重复 commit 幂等）。"""
        conn = connect(self._db_path)
        try:
            with conn:  # 短事务
                self._upsert_session(conn, session, scope)
                self._insert_session_facts(conn, session)
                conn.execute(
                    "INSERT OR IGNORE INTO review_jobs "
                    "(turn_id, session_id, scope_kind, scope_key, status, attempts, "
                    "available_at, payload_json) "
                    "VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)",
                    (
                        turn.turn_id,
                        turn.session_id,
                        scope.kind,
                        scope.key,
                        turn.finished_at,
                        turn.model_dump_json(),
                    ),
                )
        finally:
            conn.close()

    # ---- 重开：原子关闭旧 session + 创建新 session + 切换 slot（§6.3 / ADR-13）----
    def close_and_reset(
        self,
        session_key: str,
        session: Session,
        *,
        scope: MemoryScope,
        channel: str = "",
        author_id: str = "",
    ) -> ResetResult:
        """一个事务内完成状态转换；事务成功后调用方才允许发 session_end hook。

        传入的 `session` 是调用方持有的最新状态（可能含未 commit 的消息），
        先落库并标记 completed，再创建新 session 并切换 slot。
        """
        conn = connect(self._db_path)
        try:
            with conn:  # 短事务：失败整体回滚，不得提前发结束 hook
                closed = session.model_copy(update={"status": "completed"})
                self._upsert_session(conn, closed, scope)
                self._insert_session_facts(conn, closed)
                new_session = self._new_session(session_key, channel, author_id)
                self._upsert_session(conn, new_session, scope)
                conn.execute(
                    "UPDATE session_slots SET active_session_id = ?, updated_at = ? "
                    "WHERE session_key = ?",
                    (new_session.session_id, time.time(), session_key),
                )
            logger.info("重开会话 key=%s 新 id=%s", session_key, new_session.session_id)
            return ResetResult(closed_session=closed, new_session=new_session)
        finally:
            conn.close()

    # ---- 观测：连接配置健康检查（§6.1 契约的可观察面）----
    def diagnostics(self) -> dict:
        """经连接工厂开一个新连接并读回 PRAGMA，验证每个连接都满足 §6.1。"""
        conn = connect(self._db_path)
        try:
            return {
                "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                "foreign_keys": bool(
                    conn.execute("PRAGMA foreign_keys").fetchone()[0]
                ),
                "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
            }
        finally:
            conn.close()

    # ---- 内部：审计不可变事实的插入（§6.3/ADR-12）----
    @staticmethod
    def _insert_session_facts(conn, session: Session) -> None:
        """把 session 的 messages/tool_calls 作为不可变审计事实插入（幂等/冲突见下）。"""
        for m in session.messages:
            SqliteSessionStore._insert_immutable(
                conn, "messages", "message_id", m.message_id,
                m.model_dump_json(),
                (
                    "INSERT INTO messages "
                    "(message_id, session_id, seq_id, role, content_text, "
                    "created_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        m.message_id,
                        session.session_id,
                        m.seq_id,
                        m.role,
                        m.content_text,
                        m.created_at,
                        m.model_dump_json(),
                    ),
                ),
            )
        for tc in session.tool_calls:
            SqliteSessionStore._insert_immutable(
                conn, "tool_calls", "tool_call_id", tc.tool_call_id,
                tc.model_dump_json(),
                (
                    "INSERT INTO tool_calls "
                    "(tool_call_id, session_id, trajectory_id, tool_name, "
                    "status, created_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        tc.tool_call_id,
                        session.session_id,
                        tc.trajectory_id,
                        tc.tool_name,
                        tc.status,
                        tc.created_at,
                        tc.model_dump_json(),
                    ),
                ),
            )

    @staticmethod
    def _insert_immutable(conn, table, pk_column, pk_value, payload_json, insert) -> None:
        """首次 INSERT；同 payload 重提幂等成功；异 payload 报 audit_conflict 并回滚事务。"""
        row = conn.execute(
            f"SELECT payload_json FROM {table} WHERE {pk_column} = ?", (pk_value,)
        ).fetchone()
        if row is not None:
            if row[0] != payload_json:
                raise AuditConflictError(
                    f"{table}.{pk_column}={pk_value} 已存在且 payload 不同，拒绝覆盖"
                )
            return
        sql, params = insert
        conn.execute(sql, params)

    # ---- 内部：header upsert 与按子表组装 ----
    @staticmethod
    def _upsert_session(conn, session: Session, scope: MemoryScope) -> None:
        """Session header 允许更新 status/summary/time；payload 排除 messages/tool_calls。"""
        payload = json.dumps(
            session.model_dump(mode="json", exclude={"messages", "tool_calls"}),
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO sessions "
            "(session_id, session_key, scope_kind, scope_key, status, channel, "
            "author_id, created_at, updated_at, last_active_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "status = excluded.status, updated_at = excluded.updated_at, "
            "last_active_at = excluded.last_active_at, "
            "payload_json = excluded.payload_json",
            (
                session.session_id,
                session.session_key,
                scope.kind,
                scope.key,
                session.status,
                session.channel,
                session.author_id,
                session.created_at,
                session.updated_at,
                session.last_active_at,
                payload,
            ),
        )

    @staticmethod
    def _load_session(conn, session_id: str) -> Session | None:
        row = conn.execute(
            "SELECT payload_json FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        messages = [
            Message.model_validate_json(r[0])
            for r in conn.execute(
                "SELECT payload_json FROM messages WHERE session_id = ? ORDER BY seq_id",
                (session_id,),
            )
        ]
        tool_calls = [
            ToolCall.model_validate_json(r[0])
            for r in conn.execute(
                "SELECT payload_json FROM tool_calls WHERE session_id = ? "
                "ORDER BY created_at, tool_call_id",
                (session_id,),
            )
        ]
        # messages/tool_calls 只从子表组装（§6.2 单一事实源）
        return Session(**payload, messages=messages, tool_calls=tool_calls)
