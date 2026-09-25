from __future__ import annotations

from agent.storage.sqlite import RuntimeStorage
from agent.core.memory import (
    MemoryLayer,
    MemoryScope,
    MemoryValidator,
    SessionMemoryReviewer,
    SessionMemoryReviewWorker,
)
from agent.core.memory.types import MemoryProposal, TurnRecord
from agent.core.types import Message
from agent.storage.sqlite import (
    SqliteMemoryReviewCommitter,
    SqliteSessionStore,
    connect,
)


class _Extractor:
    def extract(self, turn, session):
        return [
            MemoryProposal(
                proposal_id=f"proposal-{turn.turn_id}",
                target_layer=MemoryLayer.USER,
                type="preference",
                normalized_key="language.preference",
                summary="用户偏好中文回答",
                content="用户偏好中文回答",
                keywords=("中文", "回答"),
                confidence=0.95,
                source_authority="user_explicit",
                source_message_ids=(turn.user_message_id,),
                reason_code="explicit_preference",
            )
        ]


def _commit_turn(store, session, scope, turn_id, message_id, started_at):
    message = session.add_message(
        Message(
            message_id=message_id,
            role="user",
            content_text="以后请用中文回答",
            created_at=started_at,
        )
    )
    turn = TurnRecord(
        turn_id=turn_id,
        session_id=session.session_id,
        scope=scope,
        user_message_id=message.message_id,
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=started_at,
        finished_at=started_at + 1,
        completed=True,
        interrupted=False,
        error=None,
    )
    store.commit_turn(session, turn, scope=scope)


def test_worker_atomically_commits_memory_evidence_trace_and_job(tmp_path) -> None:
    db_path = tmp_path / "agent.db"
    scope = MemoryScope(tenant_id="cli", user_id="user-1")
    store = RuntimeStorage.open(db_path, id_factory=lambda: "session-1", extractor_version="extractor-v1").sessions
    session = store.get_or_create("slot-1", scope=scope)
    _commit_turn(store, session, scope, "turn-1", "message-1", 1.0)
    ids = iter(("memory-1", "trace-item-1", "trace-1"))
    reviewer = SessionMemoryReviewer(
        _Extractor(),
        validator=MemoryValidator(id_factory=ids.__next__),
        id_factory=ids.__next__,
        clock=iter((1.0, 1.01)).__next__,
    )
    committer = RuntimeStorage.open(db_path).review_committer
    worker = SessionMemoryReviewWorker(
        store,
        reviewer,
        committer,
        extractor_version="extractor-v1",
    )

    assert worker.process_pending() == 1
    assert store.jobs.get("turn-1").status == "completed"

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_item_versions").fetchone()[0] == 1
        assert conn.execute("SELECT SUM(json_array_length(evidence_json)) FROM memory_items").fetchone()[0] == 1
        assert conn.execute("SELECT SUM(json_array_length(keywords_json)) FROM memory_items").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1
        assert conn.execute(
            "SELECT status FROM jobs WHERE id = 'turn-1'"
        ).fetchone()[0] == "completed"
    finally:
        conn.close()


def test_repeated_fact_adds_evidence_without_duplicating_memory(tmp_path) -> None:
    db_path = tmp_path / "agent.db"
    scope = MemoryScope(tenant_id="cli", user_id="user-1")
    store = RuntimeStorage.open(db_path, id_factory=lambda: "session-1", extractor_version="extractor-v1").sessions
    session = store.get_or_create("slot-1", scope=scope)
    _commit_turn(store, session, scope, "turn-1", "message-1", 1.0)
    _commit_turn(store, session, scope, "turn-2", "message-2", 3.0)
    ids = iter(
        (
            "memory-1",
            "trace-item-1",
            "trace-1",
            "memory-2",
            "trace-item-2",
            "trace-2",
        )
    )
    reviewer = SessionMemoryReviewer(
        _Extractor(),
        validator=MemoryValidator(id_factory=ids.__next__),
        id_factory=ids.__next__,
        clock=iter((1.0, 1.01, 2.0, 2.01)).__next__,
    )
    run_ids = iter(("extraction-1", "extraction-2"))
    worker = SessionMemoryReviewWorker(
        store,
        reviewer,
        RuntimeStorage.open(db_path).review_committer,
        extractor_version="extractor-v1",
    )

    assert worker.process_pending() == 2

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 1
        assert conn.execute("SELECT SUM(json_array_length(evidence_json)) FROM memory_items").fetchone()[0] == 2
        assert conn.execute(
            "SELECT decision FROM trace_items WHERE trace_id = 'trace-2'"
        ).fetchone()[0] == "skipped"
    finally:
        conn.close()
