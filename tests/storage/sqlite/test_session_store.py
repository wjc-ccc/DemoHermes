from __future__ import annotations

from agent.storage.sqlite import RuntimeStorage
from agent.core.memory import MemoryScope
from agent.core.memory.types import TurnRecord
from agent.core.types import Message
from agent.storage.sqlite import SqliteSessionStore, connect


def test_turn_facts_and_review_job_commit_atomically(tmp_path) -> None:
    db_path = tmp_path / "agent.db"
    store = RuntimeStorage.open(db_path, id_factory=lambda: "session-1").sessions
    scope = MemoryScope(tenant_id="cli", user_id="user-1")
    session = store.get_or_create("slot-1", scope=scope, channel="cli", author_id="user-1")
    user = session.add_message(Message(message_id="message-u", role="user", content_text="你好"))
    assistant = session.add_message(
        Message(message_id="message-a", role="assistant", content_text="你好")
    )
    turn = TurnRecord(
        turn_id="turn-1",
        session_id=session.session_id,
        scope=scope,
        user_message_id=user.message_id,
        assistant_message_ids=(assistant.message_id,),
        tool_call_ids=(),
        started_at=1.0,
        finished_at=2.0,
        completed=True,
        interrupted=False,
        error=None,
        precision_trace={
            "metrics": {"trace_id": "turn-1", "tokens_used": 12},
            "records": [],
            "exposed_refs": [],
            "p3_hit_refs": [],
        },
    )

    store.commit_turn(session, turn, scope=scope)
    store.commit_turn(session, turn, scope=scope)

    restored = store.find("slot-1")
    assert restored is not None
    assert [message.message_id for message in restored.messages] == ["message-u", "message-a"]
    with store.db.read() as conn:
        job_id = conn.execute(
            "SELECT batch_job_id FROM memory_review_turns WHERE turn_id='turn-1'"
        ).fetchone()[0]
    assert store.jobs.get(job_id).status == "pending"

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert {
            row[0] for row in conn.execute("SELECT DISTINCT source_type FROM messages")
        } == {"user_authored", "assistant_authored"}
    finally:
        conn.close()

    debug_turn = store.executions.get_turn("turn-1")
    assert debug_turn is not None
    assert debug_turn["precision_trace"]["metrics"]["tokens_used"] == 12
