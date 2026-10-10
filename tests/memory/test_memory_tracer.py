"""普通用户手册经冻结快照进入 Precision。"""

from agent.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.types.memory.layers import MemoryLayer
from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_scope import ensure_scope_dir
from agent.core.precision.percision_engine import PrecisionBuilder
from agent.types.conversation import Session


def test_user_book_reaches_precision_system_messages(tmp_path):
    root = tmp_path / "memory"
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    documents = MarkdownDocuments(root)
    documents.replace_user(scope, "# 沟通偏好\n\n用户偏好先给结论。\n", expected_hash=documents.read(scope).disk_hash, actor="human", reason="test setup")

    snapshot = MemoryContextLoader(root).freeze(scope, session_id="s1")
    messages = PrecisionBuilder().build(
        Session(session_id="s1"), memory_snapshot=snapshot
    ).messages

    system_texts = [message["content"] for message in messages if message["role"] == "system"]
    assert any(
        "用户偏好先给结论" in text and "<memory-context>" in text
        for text in system_texts
    )
