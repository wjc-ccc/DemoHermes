from __future__ import annotations

from agent.storage.sqlite import RuntimeStorage
from agent.core.memory import (
    MemoryScope,
    UserMemoryItem,
    content_sha256,
)
from agent.storage.sqlite import SqliteMemoryStore


def test_user_memory_store_preserves_item_and_keywords(tmp_path) -> None:
    db_path = tmp_path / "agent.db"
    store = RuntimeStorage.open(db_path).memory
    content = "用户偏好中文回答"
    item = UserMemoryItem(
        item_id="memory-1",
        kind="stable_preference",
        source_type="user_authored",
        source_ref="message-1",
        scope=MemoryScope(tenant_id="cli", user_id="user-1"),
        content=content,
        summary=content,
        slot_key="stable_preference:assistant:response_language",
        source_authority="user_explicit",
        content_hash=content_sha256(content),
        keywords=("中文", "回答"),
    )

    stored = store.put(item)

    assert stored.created_at is not None
    assert store.get("memory-1") == stored


