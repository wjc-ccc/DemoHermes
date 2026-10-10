"""SqliteSessionStore.list_sessions — 与 DictSessionStore 对齐的观测面。"""
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.conversation import Message
from agent.types.memory.scope import MemoryScope
from agent.storage.sqlite.session_store import SqliteSessionStore
from agent.types.memory.source import TurnRecord

_SCOPE = MemoryScope(tenant_id="frontier", user_id="web-user")


def _turn(session, *, turn_id="t1"):
    return TurnRecord(
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


def test_list_sessions_returns_active_slot_summaries(tmp_path):
    store = RuntimeStorage.open(tmp_path / "memory.db").sessions
    session = store.get_or_create(
        "agent:main:frontier:dm:web",
        scope=_SCOPE,
        channel="frontier",
        author_id="web-user",
    )
    session.add_message(Message(role="user", content_text="hi"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    listed = store.list_sessions()

    assert len(listed) == 1
    row = listed[0]
    assert row["session_key"] == "agent:main:frontier:dm:web"
    assert row["session_id"] == session.session_id
    assert row["channel"] == "frontier"
    assert row["author_id"] == "web-user"
    assert row["message_count"] == 1
    assert "last_active_at" in row


def test_list_sessions_empty_db(tmp_path):
    store = RuntimeStorage.open(tmp_path / "memory.db").sessions
    assert store.list_sessions() == []
