from __future__ import annotations
from agent.types.memory.mutation import MemoryMutationItem

from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_validator import MemoryValidator
from agent.core.memory.memory_batch_reviewer import MemoryBatchReviewer
from agent.core.memory.memory_review_worker import MemoryReviewWorker
from agent.types.memory.extraction import MemoryProposal, MemoryExtractionBatch
from agent.types.memory.source import TurnRecord
from agent.types.conversation import Message
from agent.core.storage.sqlite.memory_review_committer import SqliteMemoryReviewCommitter
from agent.core.storage.sqlite.session_store import SqliteSessionStore
from agent.core.storage.sqlite.database import connect
from agent.core.storage.sqlite.memory_card_mutation import SqliteMemoryCardMutationService


class _Extractor:
    def extract_batch(self, turns, sessions):
        turn, session = turns[-1], sessions[-1]
        source = next(m.content_text for m in session.messages
                      if m.message_id == turn.user_message_id)
        return MemoryExtractionBatch(proposals=(
            MemoryProposal(
                proposal_id=f"proposal-{turn.turn_id}",
                kind="stable_preference",
                subject="assistant", attribute="response_language",
                summary="用户偏好中文回答",
                content="用户偏好中文回答",
                keywords=("中文", "回答"),
                confidence=0.95,
                source_authority="user_explicit",
                source_message_ids=(turn.user_message_id,),
                evidence_quotes={turn.user_message_id: source},
                reason_code="explicit_preference",
            ),
        ))


def _commit_turn(store, session, scope, turn_id, message_id, started_at, text="以后请用中文回答"):
    message = session.add_message(
        Message(
            message_id=message_id,
            role="user",
            content_text=text,
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
    runtime = RuntimeStorage.open(db_path, id_factory=lambda: "session-1", extractor_version="extractor-v1")
    store = runtime.sessions
    session = store.get_or_create("slot-1", scope=scope)
    _commit_turn(store, session, scope, "turn-1", "message-1", 1.0)
    ids = iter(("memory-1", "trace-item-1", "trace-1"))
    reviewer = MemoryBatchReviewer(
        _Extractor(),
        validator=MemoryValidator(id_factory=ids.__next__),
        id_factory=ids.__next__,
        clock=iter((1.0, 1.01)).__next__,
    )
    committer = runtime.review_committer
    worker = MemoryReviewWorker(
        store,
        reviewer,
        committer,
        extractor_version="extractor-v1",
    )

    assert worker.process_pending() == 1
    jobs = store.jobs.list(job_type="memory.extract")
    assert len(jobs) == 1 and jobs[0].status == "completed"

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_cards").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_card_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_terms").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'completed'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_new_explicit_preference_supersedes_old_and_stale_job_cannot_restore_it(tmp_path):
    db_path = tmp_path / "agent.db"
    scope = MemoryScope(tenant_id="cli", user_id="user-1")
    runtime = RuntimeStorage.open(db_path, id_factory=lambda: "session-1",
        extractor_version="extractor-v1")
    store = runtime.sessions
    session = store.get_or_create("slot-1", scope=scope)
    _commit_turn(store, session, scope, "turn-1", "message-1", 1.0)
    _commit_turn(store, session, scope, "turn-2", "message-2", 3.0,
        text="改成英文回答")
    store.jobs.enqueue(
        job_type="memory.extract", handler_version="extractor-v1",
        input={"turn_ids": ("turn-2",)}, dedupe_key="correction:turn-2",
        session_id=session.session_id, turn_id="turn-2", job_id="correction-review-2",
    )

    class _ChangingExtractor:
        def extract_batch(self, turns, sessions):
            turn, session = turns[-1], sessions[-1]
            language = "中文" if turn.turn_id == "turn-1" else "英文"
            source = next(m.content_text for m in session.messages
                          if m.message_id == turn.user_message_id)
            return MemoryExtractionBatch(proposals=(MemoryProposal(
                proposal_id=f"proposal-{turn.turn_id}",
                kind="stable_preference",
                subject="assistant", attribute="response_language",
                summary=f"用户要求用{language}回答", content=f"以后用{language}回答",
                keywords=(language,), confidence=1.0,
                source_authority="user_explicit",
                source_message_ids=(turn.user_message_id,),
                evidence_quotes={turn.user_message_id: source},
                reason_code="explicit_language_change",
            ),))

    worker = MemoryReviewWorker(
        store, MemoryBatchReviewer(_ChangingExtractor()), runtime.review_committer,
        extractor_version="extractor-v1",
    )
    assert worker.process_pending() == 2
    cards = runtime.memory.search(scope, "")
    assert len(cards) == 1 and cards[0].content == "以后用英文回答"

    store.jobs.enqueue(
        job_type="memory.extract", handler_version="extractor-v1",
        input={"turn_ids": ("turn-1",)}, dedupe_key="stale:turn-1",
        session_id=session.session_id, turn_id="turn-1", job_id="stale-review-1",
    )
    assert worker.process_pending() == 1
    cards_after = runtime.memory.search(scope, "")
    assert len(cards_after) == 1 and cards_after[0].content == "以后用英文回答"
    with runtime.db.read() as conn:
        rows = conn.execute("SELECT status FROM memory_cards ORDER BY created_at,id").fetchall()
        conflicts = conn.execute("SELECT reason FROM memory_conflicts").fetchall()
    assert sorted(status for (status,) in rows) == ["active", "superseded"]
    assert conflicts == [("stale_or_weaker_evidence",)]


def test_deleted_fact_is_not_restored_by_old_extraction_job(tmp_path):
    db_path = tmp_path / "agent.db"
    scope = MemoryScope(tenant_id="cli", user_id="user-1")
    runtime = RuntimeStorage.open(db_path, id_factory=lambda: "session-1",
        extractor_version="extractor-v1")
    session = runtime.sessions.get_or_create("slot-1", scope=scope)
    _commit_turn(runtime.sessions, session, scope, "turn-1", "message-1", 1.0)
    worker = MemoryReviewWorker(
        runtime.sessions, MemoryBatchReviewer(_Extractor()),
        runtime.review_committer, extractor_version="extractor-v1",
    )
    assert worker.process_pending() == 1
    card = runtime.memory.search(scope, "")[0]
    _commit_turn(runtime.sessions, session, scope, "turn-2", "message-2", 3.0,
        text="忘掉这个偏好")
    service = SqliteMemoryCardMutationService(runtime.db)
    with runtime.db.write() as conn:
        service.apply(conn, scope, "message-2", MemoryMutationItem.model_validate({
            "operation": "delete", "item_id": card.item_id,
            "expected_revision": card.revision,
        }))
    runtime.sessions.jobs.enqueue(
        job_type="memory.extract", handler_version="extractor-v1",
        input={"turn_ids": ("turn-1",)}, dedupe_key="deleted-replay:turn-1",
        session_id=session.session_id, turn_id="turn-1", job_id="deleted-replay-1",
    )
    assert worker.process_pending() == 1
    assert runtime.memory.get(card.item_id).status.value == "deleted"
    with runtime.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_cards").fetchone()[0] == 1


def test_repeated_fact_adds_evidence_without_duplicating_memory(tmp_path) -> None:
    db_path = tmp_path / "agent.db"
    scope = MemoryScope(tenant_id="cli", user_id="user-1")
    runtime = RuntimeStorage.open(db_path, id_factory=lambda: "session-1", extractor_version="extractor-v1")
    store = runtime.sessions
    session = store.get_or_create("slot-1", scope=scope)
    _commit_turn(store, session, scope, "turn-1", "message-1", 1.0)
    _commit_turn(store, session, scope, "turn-2", "message-2", 3.0)
    # The scheduler waits for a third turn before its normal second batch.
    # Queue this single-turn job to exercise the committer's duplicate merge.
    store.jobs.enqueue(
        job_type="memory.extract", handler_version="extractor-v1",
        input={"turn_ids": ("turn-2",)}, dedupe_key="committer-test:turn-2",
        session_id=session.session_id, turn_id="turn-2", job_id="test-review-2",
    )
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
    reviewer = MemoryBatchReviewer(
        _Extractor(),
        validator=MemoryValidator(id_factory=ids.__next__),
        id_factory=ids.__next__,
        clock=iter((1.0, 1.01, 2.0, 2.01)).__next__,
    )
    run_ids = iter(("extraction-1", "extraction-2"))
    worker = MemoryReviewWorker(
        store,
        reviewer,
        runtime.review_committer,
        extractor_version="extractor-v1",
    )

    assert worker.process_pending() == 2

    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_cards").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0] == 2
        assert conn.execute(
            "SELECT decision FROM trace_items WHERE trace_id = 'trace-2'"
        ).fetchone()[0] == "skipped"
    finally:
        conn.close()
