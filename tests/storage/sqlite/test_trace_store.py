from __future__ import annotations

from datetime import datetime, timezone

from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.memory.trace import (
    MemoryAiTraceFields,
    MemoryItemTrace,
    MemoryRuntimeTraceFields,
    MemoryTrace,
)
from agent.types.memory.layers import MemoryLayer
from agent.types.memory.trace_categories import MemoryTraceDecision, MemoryTraceOperation
from agent.core.storage.sqlite.trace_store import SqliteTraceStore


def test_trace_round_trip_keeps_ai_and_runtime_fields_separate(tmp_path) -> None:
    store = RuntimeStorage.open(tmp_path / "agent.db").traces
    trace = MemoryTrace(
        trace_id="trace-1",
        operation=MemoryTraceOperation.VALIDATE,
        session_id="session-1",
        turn_id="turn-1",
        validator_version="v1",
        items=(
            MemoryItemTrace(
                trace_item_id="trace-item-1",
                proposal_id="proposal-1",
                layer=MemoryLayer.USER,
                type="preference",
                decision=MemoryTraceDecision.ACCEPTED,
                ai=MemoryAiTraceFields(
                    proposed_layer=MemoryLayer.USER,
                    proposed_type="preference",
                    self_reported_confidence=0.9,
                    reason_code="explicit_preference",
                ),
                runtime=MemoryRuntimeTraceFields(
                    validator_version="v1",
                    validation_codes=("accepted",),
                    token_count=8,
                ),
            ),
        ),
    )

    store.put(trace)

    assert store.get("trace-1") == trace


def test_trace_store_lists_by_session_and_item_and_applies_retention(tmp_path) -> None:
    store = RuntimeStorage.open(tmp_path / "agent.db").traces
    old = MemoryTrace(
        trace_id="trace-old",
        operation=MemoryTraceOperation.PERSIST,
        session_id="session-1",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        items=(
            MemoryItemTrace(
                trace_item_id="item-trace-old",
                item_id="memory-1",
                decision=MemoryTraceDecision.ACCEPTED,
            ),
        ),
    )
    recent = MemoryTrace(
        trace_id="trace-recent",
        operation=MemoryTraceOperation.LOAD,
        session_id="session-2",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store.put(old)
    store.put(recent)

    assert store.list_recent(session_id="session-2") == (recent,)
    assert store.list_recent(item_id="memory-1") == (old,)
    assert store.delete_before(datetime(2025, 1, 2, tzinfo=timezone.utc)) == 1
    assert store.get("trace-old") is None
    assert store.get("trace-recent") == recent
