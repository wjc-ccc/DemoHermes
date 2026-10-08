from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.core.memory import MemoryScope, UserMemoryItem, content_sha256
from agent.core.memory.types.memory_source_types import EvidenceRef, TurnRecord
from agent.core.types import Message
from agent.storage.sqlite import RuntimeStorage
from agent.storage.sqlite.memory_card_mutation import SqliteMemoryCardMutationService


def test_mutation_requires_existing_card_revision_and_scoped_source(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    other = MemoryScope(tenant_id="cli", user_id="u2")
    session = storage.sessions.get_or_create("slot", scope=scope)
    user = session.add_message(Message(
        message_id="m1", role="user", content_text="改成以后用英文回答",
        created_at=1000.0,
    ))
    storage.sessions.commit_turn(session, TurnRecord(
        turn_id="t1", session_id=session.session_id, scope=scope,
        user_message_id=user.message_id, assistant_message_ids=(), tool_call_ids=(),
        started_at=1000.0, finished_at=1001.0, completed=True,
        interrupted=False, error=None,
    ), scope=scope)
    claim = "以后用中文回答"
    card = storage.memory.put(UserMemoryItem(
        item_id="card-1", kind="stable_preference",
        slot_key="stable_preference:assistant:response_language",
        source_type="memory_extraction", source_ref="old", scope=scope,
        summary=claim, content=claim, content_hash=content_sha256(claim),
        source_authority="user_explicit",
        evidence=(EvidenceRef(source_type="message", source_id="old",
                              observed_at=datetime.fromtimestamp(1, timezone.utc)),),
    ))
    service = SqliteMemoryCardMutationService(storage.db)
    payload = {"operation": "update", "item_id": card.item_id,
               "expected_revision": card.revision,
               "summary": "以后用英文回答", "content": "以后用英文回答"}
    with storage.db.write() as conn:
        updated = service.apply(conn, scope, "m1", payload)
    assert updated.item_id == card.item_id and updated.revision == 2
    with pytest.raises(Exception, match="revision"):
        with storage.db.write() as conn:
            service.apply(conn, scope, "m1", payload)
    with pytest.raises(Exception, match="outside current user scope"):
        with storage.db.write() as conn:
            service.apply(conn, other, "m1", {**payload, "expected_revision": 2})
    with pytest.raises(ValueError, match="unsupported"):
        with storage.db.write() as conn:
            service.apply(conn, scope, "m1", {
                **payload, "operation": "set", "expected_revision": 2,
            })

