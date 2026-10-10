"""
DictSessionStore — session_key → Session 的内存字典存储

供独立 Gateway 测试使用，不落盘、不建索引文件。
应用 Runtime 始终使用 SqliteSessionStore，Memory 开关不影响会话落库。
两层标识仍然保留：
    session_key → 逻辑槽（平台+聊天+用户，由 build_session_key 生成）
    session_id  → 某次会话的 UUID（/new 或 reset 时换新）

接口与 SqliteSessionStore 一致（find / get_or_create / commit_turn /
close_and_reset / list_sessions），Gateway 只走一条持久化路径。
"""
from __future__ import annotations

import logging
import threading

from agent.types.memory.scope import MemoryScope
from agent.types.conversation import SessionReset, Session
from agent.types.memory.source import TurnRecord
from agent.types.storage import TurnCommitResult

logger = logging.getLogger(__name__)


class DictSessionStore:
    """纯内存会话存储。进程重启即清空"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._scope_keys: dict[str, str] = {}
        self._lock = threading.RLock()

    # ---- 上下文寻找：session_key → Session ----
    def find(self, session_key: str) -> Session | None:
        """按 session_key 找上下文；找不到返回 None（不自动创建）。"""
        with self._lock:
            return self._sessions.get(session_key)

    def get_or_create(
        self, session_key: str, *, scope: MemoryScope, channel: str = "", author_id: str = "",
    ) -> Session:
        """找上下文，找不到就新开一个 Session 并登记；同一槽位不允许换 scope。"""
        with self._lock:
            session = self._sessions.get(session_key)
            if session is not None:
                if self._scope_keys[session_key] != scope.storage_key:
                    raise ValueError(f"session_key={session_key} 的 scope 与请求 scope 不一致")
                return session
            return self._open(session_key, scope, channel, author_id)

    # ---- 提交：dict 中的 Session 已是同一对象，只需登记 ----
    def commit_turn(self, session: Session, turn: TurnRecord, *, scope: MemoryScope) -> TurnCommitResult:
        if turn.session_id != session.session_id:
            raise ValueError("Turn 属于其他会话")
        with self._lock:
            self._sessions[session.session_key] = session
            self._scope_keys[session.session_key] = scope.storage_key
        return TurnCommitResult(turn.turn_id, persisted=False)

    # ---- 重开：同 key 换新 session_id（等同 /new）----
    def close_and_reset(
        self, session_key: str, session: Session, *, scope: MemoryScope,
        channel: str = "", author_id: str = "",
    ) -> SessionReset:
        with self._lock:
            current = self._sessions.get(session_key)
            if current is None or current.session_id != session.session_id:
                raise ValueError("被重开的会话已经不是当前会话")
            closed = session.model_copy(update={"status": "completed"})
            new_session = self._open(session_key, scope, channel, author_id)
            return SessionReset(closed_session=closed, new_session=new_session)

    def _open(self, session_key: str, scope: MemoryScope, channel: str, author_id: str) -> Session:
        session = Session(session_key=session_key, channel=channel, author_id=author_id)
        self._sessions[session_key] = session
        self._scope_keys[session_key] = scope.storage_key
        logger.info("新开会话 key=%s id=%s", session_key, session.session_id)
        return session

    # ---- 观测：前端会话列表用 ----
    def list_sessions(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "session_key": s.session_key,
                    "session_id": s.session_id,
                    "channel": s.channel,
                    "author_id": s.author_id,
                    "message_count": s.message_count,
                    "last_active_at": s.last_active_at,
                }
                for s in self._sessions.values()
            ]
