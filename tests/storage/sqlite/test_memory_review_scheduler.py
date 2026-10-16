from __future__ import annotations

import json

from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_extractor import MemoryExtractor
from agent.types.memory.source import TurnRecord
from agent.types.conversation import Message
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.storage.sqlite.codec import milliseconds


def _commit(storage, scope, index: int, *, route: str, completed: bool = True):
    session = storage.sessions.get_or_create(route, scope=scope, channel="cli", author_id=scope.user_id or "")
    user = session.add_message(Message(
        message_id=f"user-{index}", role="user", content_text=f"第 {index} 轮"
    ))
    assistant = session.add_message(Message(
        message_id=f"assistant-{index}", role="assistant", content_text="收到"
    ))
    turn = TurnRecord(
        turn_id=f"turn-{index}", session_id=session.session_id,
        scope=scope.model_copy(update={"session_id": session.session_id}),
        user_message_id=user.message_id,
        assistant_message_ids=(assistant.message_id,), tool_call_ids=(),
        started_at=float(index), finished_at=float(index) + 0.1,
        completed=completed, interrupted=False,
        error=None if completed else "failed",
    )
    storage.sessions.commit_turn(session, turn, scope=scope)
    return session


def _batches(storage):
    with storage.db.read() as conn:
        return [json.loads(row[0])["turn_ids"] for row in conn.execute(
            "SELECT input_json FROM jobs WHERE job_type='memory.extract' "
            "ORDER BY created_at,id"
        )]


def test_progressive_cadence_survives_restart_and_cross_session(tmp_path):
    path = tmp_path / "agent.db"
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    storage = RuntimeStorage.open(path)
    for number in range(1, 11):
        _commit(storage, scope, number, route=f"slot-{number % 2}")
    assert sorted(map(len, _batches(storage))) == [1, 2, 3, 4]

    storage = RuntimeStorage.open(path)
    _commit(storage, scope, 99, route="slot-1", completed=False)
    for number in range(11, 21):
        _commit(storage, scope, number, route=f"slot-{number % 2}")
    batches = _batches(storage)
    assert sorted(map(len, batches)) == [1, 2, 3, 4, 5, 5]
    assert {turn_id for batch in batches for turn_id in batch} == {
        f"turn-{number}" for number in range(1, 21)
    }
    assert all("turn-99" not in batch for batch in batches)
    with storage.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_review_turns").fetchone()[0] == 20


def test_reset_flushes_remainder_without_advancing_stage(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    session = _commit(storage, scope, 1, route="slot")
    session = _commit(storage, scope, 2, route="slot")
    assert sorted(map(len, _batches(storage))) == [1]
    storage.sessions.close_and_reset("slot", session, scope=scope)
    assert sorted(map(len, _batches(storage))) == [1, 1]
    _commit(storage, scope, 3, route="slot")
    assert sorted(map(len, _batches(storage))) == [1, 1, 1]


def test_shutdown_flush_is_idempotent(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    _commit(storage, scope, 1, route="slot")
    _commit(storage, scope, 2, route="slot")
    assert storage.review_scheduler.flush_all() == 1
    assert storage.review_scheduler.flush_all() == 0
    assert sorted(map(len, _batches(storage))) == [1, 1]


def test_later_batch_waits_for_earlier_retry(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    for number in range(1, 4):
        _commit(storage, scope, number, route="slot")
    now = milliseconds() + 1000
    first = storage.jobs.claim(
        job_type="memory.extract", handler_version=MemoryExtractor.version,
        worker_id="test", now=now, lease_ms=10_000,
    )
    assert first is not None
    storage.jobs.fail(first, "temporary", now=now, available_at=now + 10_000)
    assert storage.jobs.claim(
        job_type="memory.extract", handler_version=MemoryExtractor.version,
        worker_id="test", now=now, lease_ms=10_000,
    ) is None
    first = storage.jobs.claim(
        job_type="memory.extract", handler_version=MemoryExtractor.version,
        worker_id="test", now=now + 10_001, lease_ms=10_000,
    )
    assert first is not None
    storage.jobs.complete(first, {}, now=now + 10_001)
    second = storage.jobs.claim(
        job_type="memory.extract", handler_version=MemoryExtractor.version,
        worker_id="test", now=now + 10_002, lease_ms=10_000,
    )
    assert second is not None and second.id != first.id


def test_terminal_failure_does_not_block_later_batch(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    for number in range(1, 4):
        _commit(storage, scope, number, route="slot")
    now = milliseconds() + 1000
    first = storage.jobs.claim(
        job_type="memory.extract", handler_version=MemoryExtractor.version,
        worker_id="test", now=now, lease_ms=10_000,
    )
    assert first is not None
    failed = storage.jobs.fail(first, "model unavailable", now=now,
                               available_at=now + 1000, max_attempts=1)
    assert failed.status == "terminal_failed"
    second = storage.jobs.claim(
        job_type="memory.extract", handler_version=MemoryExtractor.version,
        worker_id="test", now=now, lease_ms=10_000,
    )
    assert second is not None and second.id != first.id

