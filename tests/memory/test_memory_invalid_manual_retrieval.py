from __future__ import annotations

from agent.core.memory import MemoryContextLoader, MemoryManualEditor, MemoryScope
from agent.core.memory.memory_retrieval import MemoryRetriever
from agent.storage.sqlite import RuntimeStorage


def test_invalid_external_manual_is_not_exposed_by_raw_search_or_read(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    storage.sessions.get_or_create("slot", scope=scope)
    root = tmp_path / "memory"
    editor = MemoryManualEditor(root, storage.db, max_tokens=20)
    initial = editor.read(scope)
    editor.replace(scope, "# 沟通偏好\n简洁回答", expected_hash=initial.disk_hash,
                   actor="model", reason="记录稳定偏好", source_ref="message:source")
    loader = MemoryContextLoader(root, manual_editor=editor, document_registry=storage.documents)
    loader.freeze(scope, session_id="first")
    with storage.db.read() as conn:
        document_id = conn.execute(
            "SELECT id FROM context_documents WHERE layer='user' AND scope_key=?",
            (scope.storage_key,),
        ).fetchone()[0]
    path = root / scope.storage_key / "MEMORY.md"
    path.write_text("错误内容" * 100, encoding="utf-8")
    snapshot = loader.freeze(scope, session_id="second")
    assert snapshot.diagnostics and snapshot.diagnostics[0].code == "memory_document_invalid"
    retriever = MemoryRetriever(storage.db, document_root=root)
    assert retriever.search(scope, "错误内容", source="raw") == ()
    page = retriever.read(scope, f"document:{document_id}")
    assert page is not None and "简洁回答" in page.content
    assert "错误内容" not in page.content
