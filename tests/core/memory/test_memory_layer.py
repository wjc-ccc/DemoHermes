from __future__ import annotations

import pytest

from agent.core.memory import (
    MemoryLayer,
    MemoryScope,
    UserMemoryItem,
    authority_rank,
    automatic_promotion_allowed,
    content_sha256,
)


def test_five_layers_have_stable_authority_order() -> None:
    assert [authority_rank(layer) for layer in MemoryLayer] == [0, 1, 2, 3, 4]
    assert automatic_promotion_allowed(MemoryLayer.SCRATCHPAD, MemoryLayer.TASK)
    assert automatic_promotion_allowed(MemoryLayer.TASK, MemoryLayer.USER)
    assert not automatic_promotion_allowed(MemoryLayer.SCRATCHPAD, MemoryLayer.USER)


def test_item_type_is_checked_by_layer() -> None:
    content = "用户偏好中文回答"
    item = UserMemoryItem(
        item_id="m-1",
        type="preference",
        source_type="manual",
        source_ref="message-1",
        scope=MemoryScope(tenant_id="local", user_id="u-1"),
        content=content,
        summary=content,
        normalized_key="language.preference",
        source_authority="user_explicit",
        content_hash=content_sha256(content),
    )
    assert item.layer is MemoryLayer.USER

    with pytest.raises(ValueError, match="不是 user 层允许"):
        UserMemoryItem(**{**item.model_dump(), "type": "goal"})
