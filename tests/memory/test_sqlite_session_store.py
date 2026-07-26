"""Slice 9: SQLite 最小恢复 tracer bullet——跨 Store 实例恢复 Session（§6）。

公共 seam：SessionStore（SqliteSessionStore）。真实文件 SQLite，不 mock sqlite3。
"""
from Agent.core.types import Message, Session
from Agent.memory import MemoryScope
from Agent.memory.episodic.store import SqliteSessionStore
from Agent.memory.types import TurnEnvelope

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")


def _turn(session: Session, *, turn_id: str = "t1") -> TurnEnvelope:
    return TurnEnvelope(
        turn_id=turn_id,
        session_id=session.session_id,
        scope=_SCOPE,
        user_message_id=session.messages[-1].message_id,
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_saved_session_is_restored_by_new_store_instance(tmp_path):
    """commit_turn 落库后，新 Store 实例（=进程重启）能按 session_key 恢复 Session。"""
    db = tmp_path / "memory.db"
    store1 = SqliteSessionStore(db)
    session = store1.get_or_create(
        "cli:dm:u1", scope=_SCOPE, channel="cli", author_id="u1"
    )
    session.add_message(Message(role="user", content_text="你好，记住我"))
    store1.commit_turn(session, _turn(session), scope=_SCOPE)

    store2 = SqliteSessionStore(db)  # 新实例，模拟重启
    restored = store2.find("cli:dm:u1")

    assert restored is not None
    assert restored.session_id == session.session_id
    assert restored.session_key == "cli:dm:u1"
    assert restored.channel == "cli"
    assert restored.author_id == "u1"
    assert [m.content_text for m in restored.messages] == ["你好，记住我"]
    assert restored.messages[0].message_id == session.messages[0].message_id
    assert restored.message_count == 1


def test_repeated_commit_turn_is_idempotent(tmp_path):
    """§6.3：相同 turn_id + 相同 payload 重复 commit 幂等成功，消息不翻倍。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="问题"))
    turn = _turn(session)

    store.commit_turn(session, turn, scope=_SCOPE)
    store.commit_turn(session, turn, scope=_SCOPE)  # 重复提交同一 turn

    restored = SqliteSessionStore(db).find("k")
    assert restored is not None
    assert [m.content_text for m in restored.messages] == ["问题"]
    assert restored.message_count == 1


def test_find_returns_none_for_unknown_session_key(tmp_path):
    """未知 session_key 不自动创建，返回 None。"""
    store = SqliteSessionStore(tmp_path / "memory.db")
    store.get_or_create("known", scope=_SCOPE)

    assert store.find("unknown") is None
    assert store.find("unknown") is None  # 重复 find 也不产生副作用


def test_wal_and_foreign_keys_are_enabled(tmp_path):
    """§6.1：连接工厂产生的每个连接都满足 WAL / foreign_keys / busy_timeout 契约。"""
    store = SqliteSessionStore(tmp_path / "memory.db")

    info = store.diagnostics()

    assert info["journal_mode"] == "wal"
    assert info["foreign_keys"] is True
    assert info["busy_timeout"] == 5000
