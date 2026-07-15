"""
SessionStore — session_key 索引 + transcript 落盘（JSONL 实现）

两层标识：
    session_key  → 逻辑槽（平台+聊天+用户）
    session_id   → 某次 transcript UUID

存储建议见模块末尾与 docs/aiDocs/7.14：
    现在用 JsonlSessionStore；日后可换成 Redis(index 热) + SQLite(transcript)。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

from ..core.types import Session, Message
from ..Provider import ROOT

logger = logging.getLogger(__name__)

DEFAULT_SESSIONS_DIR = ROOT / "data" / "sessions"


class SessionStore(ABC):
    @abstractmethod
    def get_or_create(self, session_key: str, *, channel: str = "", author_id: str = "") -> Session:
        ...

    @abstractmethod
    def save(self, session: Session) -> None:
        """持久化 index + 全量/增量 transcript。"""
        ...

    @abstractmethod
    def load(self, session_key: str) -> Session | None:
        ...

    @abstractmethod
    def reset(self, session_key: str, *, channel: str = "", author_id: str = "") -> Session:
        """同 key 换新 session_id（等同 /new）。"""
        ...


class JsonlSessionStore(SessionStore):
    """
    布局：
        data/sessions/index.json
        data/sessions/transcripts/{session_id}.jsonl   # 每行一条 Message JSON
    """

    def __init__(self, root: Path | None = None):
        self.root = root or DEFAULT_SESSIONS_DIR
        self.index_path = self.root / "index.json"
        self.transcript_dir = self.root / "transcripts"
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, Session] = {}
        self._index: dict[str, dict] = self._load_index()

    def _load_index(self) -> dict[str, dict]:
        if not self.index_path.exists():
            return {}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("failed to load session index")
            return {}

    def _write_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _transcript_path(self, session_id: str) -> Path:
        return self.transcript_dir / f"{session_id}.jsonl"

    def _load_messages(self, session_id: str) -> list[Message]:
        path = self._transcript_path(session_id)
        if not path.exists():
            return []
        messages: list[Message] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # pydantic v1: parse_raw；v2: model_validate_json
                if hasattr(Message, "model_validate_json"):
                    messages.append(Message.model_validate_json(line))
                else:
                    messages.append(Message.parse_raw(line))
        return messages

    def _msg_json(self, msg: Message) -> str:
        if hasattr(msg, "model_dump_json"):
            return msg.model_dump_json()
        return msg.json()

    def _rewrite_transcript(self, session: Session) -> None:
        path = self._transcript_path(session.session_id)
        with path.open("w", encoding="utf-8") as f:
            for msg in session.messages:
                f.write(self._msg_json(msg) + "\n")

    def load(self, session_key: str) -> Session | None:
        with self._lock:
            if session_key in self._cache:
                return self._cache[session_key]
            meta = self._index.get(session_key)
            if not meta:
                return None
            session = Session(
                session_id=meta["session_id"],
                session_key=session_key,
                channel=meta.get("channel", ""),
                author_id=meta.get("author_id", ""),
                created_at=meta.get("created_at", time.time()),
                updated_at=meta.get("updated_at", time.time()),
                last_active_at=meta.get("last_active_at", time.time()),
                messages=self._load_messages(meta["session_id"]),
            )
            session.message_count = len(session.messages)
            self._cache[session_key] = session
            return session

    def get_or_create(self, session_key: str, *, channel: str = "", author_id: str = "") -> Session:
        with self._lock:
            existing = self.load(session_key)
            if existing is not None:
                return existing
            session = Session(
                session_key=session_key,
                channel=channel,
                author_id=author_id,
            )
            self._index[session_key] = {
                "session_id": session.session_id,
                "channel": channel,
                "author_id": author_id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "last_active_at": session.last_active_at,
            }
            self._write_index()
            self._cache[session_key] = session
            self._rewrite_transcript(session)
            logger.info("created session key=%s id=%s", session_key, session.session_id)
            return session

    def save(self, session: Session) -> None:
        with self._lock:
            key = session.session_key
            if not key:
                raise ValueError("session.session_key is required before save")
            self._index[key] = {
                "session_id": session.session_id,
                "channel": session.channel,
                "author_id": session.author_id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "last_active_at": session.last_active_at,
                "message_count": session.message_count,
            }
            self._write_index()
            self._rewrite_transcript(session)
            self._cache[key] = session

    def reset(self, session_key: str, *, channel: str = "", author_id: str = "") -> Session:
        with self._lock:
            self._cache.pop(session_key, None)
            session = Session(
                session_key=session_key,
                channel=channel,
                author_id=author_id,
            )
            self._index[session_key] = {
                "session_id": session.session_id,
                "channel": channel,
                "author_id": author_id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "last_active_at": session.last_active_at,
            }
            self._write_index()
            self._cache[session_key] = session
            self._rewrite_transcript(session)
            logger.info("reset session key=%s new_id=%s", session_key, session.session_id)
            return session


class MemorySessionStore(SessionStore):
    """纯内存，测试用。"""

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def get_or_create(self, session_key: str, *, channel: str = "", author_id: str = "") -> Session:
        with self._lock:
            if session_key not in self._sessions:
                self._sessions[session_key] = Session(
                    session_key=session_key,
                    channel=channel,
                    author_id=author_id,
                )
            return self._sessions[session_key]

    def save(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.session_key] = session

    def load(self, session_key: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_key)

    def reset(self, session_key: str, *, channel: str = "", author_id: str = "") -> Session:
        with self._lock:
            session = Session(session_key=session_key, channel=channel, author_id=author_id)
            self._sessions[session_key] = session
            return session
