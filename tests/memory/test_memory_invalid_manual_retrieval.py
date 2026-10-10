from __future__ import annotations

from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.types.memory.scope import MemoryScope
from agent.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.storage.sqlite.runtime_storage import RuntimeStorage


def test_invalid_external_manual_is_not_exposed_by_raw_search_or_read(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    storage.sessions.get_or_create("slot", scope=scope)
    root = tmp_path / "memory"
    editor = MarkdownDocuments(root, storage.db, max_tokens=20)
    initial = editor.read(scope)
    editor.replace_user(scope, "# 沟通偏好\n简洁回答", expected_hash=initial.disk_hash,
                   actor="model", reason="记录稳定偏好", source_ref="message:source")
    loader = MemoryContextLoader(root, documents=editor, document_registry=storage.documents)
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
    retriever = MemoryRetriever(storage.db, document_root=root, documents=editor)
    assert retriever.search(scope, "错误内容", source="raw") == ()
    page = retriever.read(scope, f"document:{document_id}")
    assert page is not None and "简洁回答" in page.content
    assert "错误内容" not in page.content


def test_valid_external_edit_is_consistent_across_all_readers(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="u1")
    root = tmp_path / "memory"
    documents = MarkdownDocuments(root, storage.db)
    loader = MemoryContextLoader(root, documents=documents, document_registry=storage.documents)
    loader.freeze(scope, session_id="first")
    with storage.db.read() as conn:
        identity = conn.execute("SELECT id FROM context_documents WHERE scope_key=?", (scope.storage_key,)).fetchone()[0]
    path = root / scope.storage_key / "MEMORY.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# 新偏好\n中文回答", encoding="utf-8")
    retriever = MemoryRetriever(storage.db, document_root=root, documents=documents)
    # 尚未冻结登记的有效人工修改，也必须经过同一校验并返回相同正文。
    page = retriever.read(scope, f"document:{identity}")
    assert page.content == documents.read(scope, persist=False).snapshot.content
    assert "中文回答" in loader.freeze(scope, session_id="second").prompt
    assert retriever.search(scope, "中文回答", source="raw")[0].reference == f"document:{identity}"
