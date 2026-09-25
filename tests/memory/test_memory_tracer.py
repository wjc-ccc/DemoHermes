"""普通用户手册经冻结快照进入 Precision。"""

from agent.core.memory import (
    MarkdownBook,
    MemoryContextLoader,
    MemoryLayer,
    MemoryScope,
    ensure_scope_dir,
)
from agent.core.precision import PrecisionBuilder
from agent.core.types import Session


def test_user_book_reaches_precision_system_messages(tmp_path):
    root = tmp_path / "memory"
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    MarkdownBook(
        ensure_scope_dir(root, scope) / "MEMORY.md",
        layer=MemoryLayer.USER,
        type="long_term_fact",
        scope=scope,
        writable=True,
    ).replace("# 沟通偏好\n\n用户偏好先给结论。\n")

    snapshot = MemoryContextLoader(root).freeze(scope, session_id="s1")
    messages = PrecisionBuilder().build(
        Session(session_id="s1"), memory_snapshot=snapshot
    ).messages

    system_texts = [message["content"] for message in messages if message["role"] == "system"]
    assert any(
        "用户偏好先给结论" in text and "<memory-context>" in text
        for text in system_texts
    )
