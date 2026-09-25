"""
DictSessionStore — session_key → Session 的内存字典存储

现阶段的唯一存储实现：一个 dict 搞定，不落盘、不建索引文件。
两层标识仍然保留：
    session_key → 逻辑槽（平台+聊天+用户，由 build_session_key 生成）
    session_id  → 某次会话的 UUID（/new 或 reset 时换新）

日后需要持久化时，按同样四个方法（get_or_create / find / save / reset）
换成 SQLite/Redis 实现即可，Gateway 不用改。
"""
from __future__ import annotations

import logging
import threading

from ..core.types import Session

logger = logging.getLogger(__name__)


class DictSessionStore:
    """纯内存会话存储。进程重启即清空"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    # ---- 上下文寻找：session_key → Session ----
    def find(self, session_key: str) -> Session | None:
        """按 session_key 找上下文；找不到返回 None（不自动创建）。"""
        with self._lock:
            return self._sessions.get(session_key)

    def get_or_create(self, session_key: str, *, channel: str = "", author_id: str = "") -> Session:
        """找上下文，找不到就新开一个 Session 并登记。"""
        with self._lock:
            if session_key in self._sessions:
                return self._sessions[session_key]
            session = Session(session_key=session_key, channel=channel, author_id=author_id)
            self._sessions[session_key] = session
            logger.info("新建会话 key=%s id=%s", session_key, session.session_id)
            return session

    # ---- 写回（dict 语义下主要是更新时间戳，接口为日后持久化保留）----
    def save(self, session: Session) -> None:
        with self._lock:
            if not session.session_key:
                raise ValueError("session.session_key 不能为空")
            self._sessions[session.session_key] = session

    # ---- 重开：同 key 换新 session_id（等同 /new）----
    def reset(self, session_key: str, *, channel: str = "", author_id: str = "") -> Session:
        with self._lock:
            session = Session(session_key=session_key, channel=channel, author_id=author_id)
            self._sessions[session_key] = session
            logger.info("重开会话 key=%s 新 id=%s", session_key, session.session_id)
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
