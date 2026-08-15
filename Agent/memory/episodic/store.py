"""SqliteSessionStore - 会话与对话回合的 SQLite 持久化存储

episodic 类别能力限定： 把一次会话(session)及其每轮对话(turn)的事实落进 SQLite，重启可完整恢复
	 session_key -> active session 的槽位映射：一个 key 同一时刻只指向一个活跃会话
	 turn 的 messages / tool_calls 作为不可变审计事实插入（同 payload 幂等，异 payload 拒绝覆盖）
	 review outbox：每个 turn 同事务入队一条待审任务，供后台 Reviewer 认领/完成/失败/重排
	 只读历史检索器：按 scope + casefold 子串召回历史消息候选（不冒充 FTS）

SqliteSessionStore 是 SessionStore seam 的 SQLite 适配器（与内存版 DictSessionStore 同一接口）：
每个公共方法自开自关一个连接，with conn: 包一个短事务，单进程多线程经连接工厂各取连接；
事实与 review job 写在同一事务里，消除"事实已提交但入队前崩溃"的漏审窗口；
sessions.payload_json 只存 Session 自身字段（排除 messages/tool_calls），
恢复时从子表按 seq 组装 messages——单一事实源，避免 header 与子表两份数据打架。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import NamedTuple

from ...core.types import Message, Session, ToolCall
from ..types import MemoryScope, SessionReset, TurnRecord
from .db import connect
from .schema import ensure_tables

logger = logging.getLogger(__name__)


## 检索候选：历史消息的只读投影（索引列，不把 SQL 细节泄露给上层）
class ArchivedMessage(NamedTuple):
    """archive reader 返回的检索候选：索引列投影，不含 SQL 细节。"""

    session_id: str
    message_id: str
    content_text: str
    created_at: float


## 调度任务：review outbox 一条待审作业的类型化投影（不含 payload_json 原文）
class ReviewJob(NamedTuple):
    """review outbox 中一条任务的类型化投影（§8.2）；不含 payload_json 原文。"""

    turn_id: str
    session_id: str
    scope_kind: str
    scope_key: str
    status: str
    attempts: int
    available_at: float
    lease_until: float | None
    claimed_by: str | None
    last_error: str | None


### LIKE 字面量转义：% _ \ 先转义，让通配符当普通字符参与 LIKE ... ESCAPE '\'
def _escape_like(text: str) -> str:
    r"""§6.4：`\`、`%`、`_` 必须先转义按字面量搜索（配合 LIKE ... ESCAPE '\'）。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


### 只读历史检索器：与 Store 共用一个库，只 SELECT 不产生任何写副作用
class SqliteSessionArchiveReader:
    r"""只读 SessionArchiveReader（§2）：与 SqliteSessionStore 共用数据库与连接工厂。

    检索契约（§6.4）：scope 约束的参数化 `LIKE ... ESCAPE '\\'` 预过滤；
    `%`/`_` 按字面量；大小写由 py_casefold 锁定为 Unicode casefold，
    不依赖 SQLite build 的默认 collation；普通 B-tree 索引不冒充 FTS（ADR-8）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    def search_message_candidates(
        self, scope: MemoryScope, wanted: str, limit: int
    ) -> list[ArchivedMessage]:
        """返回 scope 内 casefold 子串命中的候选（wanted 须已 normalize+casefold）。"""
        if not self._db_path.exists():
            return []  # 无库即无候选；不创建文件（只读 seam 不产生副作用）
        pattern = f"%{_escape_like(wanted)}%"
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT m.session_id, m.message_id, m.content_text, m.created_at "
                "FROM messages m JOIN sessions s ON m.session_id = s.session_id "
                "WHERE s.scope_kind = ? AND s.scope_key = ? "
                "AND py_casefold(m.content_text) LIKE ? ESCAPE '\\' "
                "ORDER BY m.created_at DESC LIMIT ?",
                (scope.kind, scope.key, pattern, limit),
            ).fetchall()
        finally:
            conn.close()
        return [ArchivedMessage(*row) for row in rows]


### 两类异常：审计冲突 / 会话完整性——都禁止静默覆盖，抛出让上层决策
class AuditConflictError(Exception):
    """§6.3/ADR-12：已完成 Message/ToolCall 以不同 payload 重提--暴露冲突，禁止覆盖。"""

    error_code = "audit_conflict"


class SessionIntegrityError(Exception):
    """§6.3：slot / session / scope 三者不一致--拒绝跨 scope 恢复。"""

    error_code = "session_integrity_error"


### SQLite 会话存储主体：会话生命周期 + turn 持久化 + review outbox 调度
class SqliteSessionStore:
    """SQLite 会话存储：单进程多线程经连接工厂各取连接（§6.1）。

    三类职责：会话生命周期（find/get_or_create/close_and_reset）、
    turn 持久化（commit_turn）、review outbox 调度（claim/complete/fail）。
    每个公共方法自开自关连接，with conn: 包短事务。
    """

    def __init__(self, db_path: str | Path, *, id_factory=None) -> None:
        self._db_path = Path(db_path)
        # session_id 生成器注入点（外部 seam，§10.1-8 测试可塞 Fake 控制幂等）；不传则 uuid4
        self._id_factory = id_factory
        conn = connect(self._db_path)
        try:
            ensure_tables(conn)
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

    # ---- 上下文寻找：session_key -> Session ----
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

    def get_session(self, session_id: str) -> Session | None:
        """按 session_id 加载 Session（含 messages）；供 Reviewer Extractor 读 turn 原文。"""
        conn = connect(self._db_path)
        try:
            return self._load_session(conn, session_id)
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
                    # §6.3：slot/session/scope 三者不一致 -> 完整性错误，不跨 scope 恢复
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
        self, session: Session, turn: TurnRecord, *, scope: MemoryScope
    ) -> None:
        """单事务保存 Session 增量 + 本 turn 的 messages/tool_calls + review job（ADR-11）。

        事实与 outbox 同事务，消除"事实已提交但入队前崩溃"的漏审窗口。
        messages/tool_calls 走审计不可变插入（同 payload 幂等，异 payload 抛 AuditConflictError）；
        review job 用 INSERT OR IGNORE + turn_id 幂等。
        """
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
    ) -> SessionReset:
        """一个事务内：落库旧 session（含未 commit 消息）并标 completed -> 建新 session -> 切换 slot（ADR-13）。

        传入的 `session` 是调用方持有的最新状态；事务成功后调用方才允许发 session_end hook，
        失败整体回滚不得提前发 hook。旧 session/messages/tool_calls 永不删除。
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
            return SessionReset(closed_session=closed, new_session=new_session)
        finally:
            conn.close()

    # ---- 观测：连接配置健康检查 + outbox 状态查询（§6.1 契约的可观察面）----
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

    def pending_review_jobs(self) -> list[ReviewJob]:
        """返回 status='pending' 的 review job（§8.2 outbox 观察面）。"""
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT turn_id, session_id, scope_kind, scope_key, status, attempts, "
                "available_at, lease_until, claimed_by, last_error "
                "FROM review_jobs WHERE status = 'pending' ORDER BY available_at"
            ).fetchall()
        finally:
            conn.close()
        return [ReviewJob(*row) for row in rows]

    def claim_review_job(
        self, *, worker_id: str, now: float, lease_seconds: float
    ) -> "tuple[ReviewJob, TurnRecord] | None":
        """§8.2：原子认领一个 pending job（status->running, attempts+1, lease_until）。

        也回收 lease 已过期的 running job--进程崩溃后其他 worker 据此接管（at-least-once）。
        返回 (ReviewJob, TurnRecord) 或 None（无可领取任务）。
        """
        conn = connect(self._db_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT turn_id, session_id, scope_kind, scope_key, status, attempts, "
                    "available_at, lease_until, claimed_by, last_error, payload_json "
                    "FROM review_jobs "
                    "WHERE (status = 'pending' AND available_at <= ?) "
                    "OR (status = 'running' AND lease_until IS NOT NULL AND lease_until < ?) "
                    "ORDER BY available_at LIMIT 1",
                    (now, now),
                ).fetchone()
                if row is None:
                    return None
                turn_id = row[0]
                lease_until = now + lease_seconds
                conn.execute(
                    "UPDATE review_jobs SET status = 'running', attempts = attempts + 1, "
                    "lease_until = ?, claimed_by = ? WHERE turn_id = ?",
                    (lease_until, worker_id, turn_id),
                )
                job = ReviewJob(
                    turn_id=row[0], session_id=row[1], scope_kind=row[2], scope_key=row[3],
                    status="running", attempts=row[5] + 1, available_at=row[6],
                    lease_until=lease_until, claimed_by=worker_id, last_error=row[9],
                )
                turn = TurnRecord.model_validate_json(row[10])
        finally:
            conn.close()
        return job, turn

    def complete_review_job(self, turn_id: str) -> None:
        """§8.2：标记 job done。"""
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE review_jobs SET status = 'done' WHERE turn_id = ?",
                    (turn_id,),
                )
        finally:
            conn.close()

    def fail_review_job(
        self, turn_id: str, error: str, *, next_available_at: float, max_attempts: int
    ) -> None:
        """§8.2：失败有界退避回 pending；超 max_attempts 标 failed。"""
        conn = connect(self._db_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT attempts FROM review_jobs WHERE turn_id = ?", (turn_id,)
                ).fetchone()
                if row is None:
                    return
                if row[0] >= max_attempts:
                    conn.execute(
                        "UPDATE review_jobs SET status = 'failed', last_error = ? "
                        "WHERE turn_id = ?",
                        (error, turn_id),
                    )
                else:
                    conn.execute(
                        "UPDATE review_jobs SET status = 'pending', last_error = ?, "
                        "available_at = ? WHERE turn_id = ?",
                        (error, next_available_at, turn_id),
                    )
        finally:
            conn.close()

    def requeue_review_job(self, turn_id: str, *, available_at: float = 0.0) -> None:
        """把 job 重置为 pending（模拟重复投递 / lease 回收后重排，§8.2）。"""
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE review_jobs SET status = 'pending', available_at = ?, "
                    "lease_until = NULL, claimed_by = NULL WHERE turn_id = ?",
                    (available_at, turn_id),
                )
        finally:
            conn.close()

    def review_job_status(self, turn_id: str) -> str | None:
        """观察某 turn 的 review job 状态（§8.2 测试观察面）。"""
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT status FROM review_jobs WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else row[0]

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
        """从 sessions.payload_json + 子表按 seq 组装恢复 Session（单一事实源，ADR-6）。"""
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
