from __future__ import annotations

import pytest

from agent.types.memory.layers import MemoryLayer, authority_rank
from agent.types.memory.scope import MemoryScope
from agent.types.memory.items import UserMemoryItem, content_sha256


def test_five_layers_have_stable_authority_order() -> None:
    assert [authority_rank(layer) for layer in MemoryLayer] == [0, 1, 2, 3, 4]


def test_item_type_is_checked_by_layer() -> None:
    content = "用户偏好中文回答"
    item = UserMemoryItem(
        item_id="m-1",
        kind="stable_preference",
        source_type="manual",
        source_ref="message-1",
        scope=MemoryScope(tenant_id="local", user_id="u-1"),
        content=content,
        summary=content,
        slot_key="stable_preference:assistant:response_language",
        source_authority="user_explicit",
        content_hash=content_sha256(content),
    )
    assert item.layer is MemoryLayer.USER

    with pytest.raises(ValueError, match="kind"):
        UserMemoryItem(**{**item.model_dump(), "kind": "goal"})
