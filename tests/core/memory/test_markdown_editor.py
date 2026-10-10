from __future__ import annotations

import pytest

from agent.types.memory.document import MarkdownRevisionConflictError
from agent.types.memory.document import MemoryManualContentError, MemoryManualFileInvalidError, MemoryManualScopeError
from agent.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.types.memory.scope import MemoryScope
from agent.storage.sqlite.runtime_storage import RuntimeStorage


def _editor(tmp_path, *, max_tokens=1200):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    return MarkdownDocuments(tmp_path / "memory", storage.db, max_tokens=max_tokens), storage


def _scope():
    return MemoryScope(tenant_id="cli", user_id="alice")


def _versions(storage, scope):
    with storage.db.read() as conn:
        return conn.execute(
            "SELECT revision,content,actor,reason,source_ref FROM memory_document_versions "
            "WHERE scope_key=? ORDER BY revision",
            (scope.storage_key,),
        ).fetchall()


def test_model_edit_is_versioned_and_readable_after_reopen(tmp_path):
    editor, storage = _editor(tmp_path)
    scope = _scope()
    first = editor.read(scope)
    assert first.snapshot.content == ""
    assert first.diagnostics == ()

    changed = editor.replace_user(
        scope, "# 偏好\n\n请用中文回答。\n", expected_hash=first.snapshot.content_hash,
        actor="model", reason="用户明确提出", source_ref="turn-1",
    )
    assert changed.content == editor.read(scope).snapshot.content
    assert (tmp_path / "memory" / scope.storage_key / "MEMORY.md").read_text(encoding="utf-8") == changed.content

    reopened = MarkdownDocuments(tmp_path / "memory", storage.db)
    assert reopened.read(scope).snapshot.content_hash == changed.content_hash
    assert _versions(storage, scope) == [
        (1, "", "system", "bootstrap", None),
        (2, changed.content, "model", "用户明确提出", "turn-1"),
    ]


def test_external_edit_is_registered_and_stale_model_edit_conflicts(tmp_path):
    editor, storage = _editor(tmp_path)
    scope = _scope()
    initial_hash = editor.read(scope).snapshot.content_hash
    path = tmp_path / "memory" / scope.storage_key / "MEMORY.md"
    path.parent.mkdir(parents=True)
    path.write_text("# 人工修订\n优先使用英文。\n", encoding="utf-8")

    current = editor.read(scope)
    assert current.snapshot.content.startswith("# 人工修订")
    assert current.revision == 2
    assert current.diagnostics == ()
    with pytest.raises(MarkdownRevisionConflictError):
        editor.replace_user(
            scope, "旧模型写入", expected_hash=initial_hash,
            actor="model", reason="旧请求", source_ref="turn-1",
        )
    assert _versions(storage, scope)[-1][2:4] == ("human", "external_edit")
    assert path.read_text(encoding="utf-8").startswith("# 人工修订")


def test_manual_edit_does_not_create_card_or_sync_job(tmp_path):
    editor, storage = _editor(tmp_path)
    scope = _scope()
    current = editor.read(scope)
    editor.replace_user(scope, "# 偏好\n请用中文回答。\n",
                   expected_hash=current.disk_hash, actor="model", reason="用户要求",
                   source_ref="message:m1")
    with storage.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_cards").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_invalid_external_edit_falls_back_and_explicit_restore_recovers(tmp_path):
    editor, storage = _editor(tmp_path, max_tokens=20)
    scope = _scope()
    first = editor.read(scope)
    good = editor.replace_user(
        scope, "# 手册\n请用中文。\n", expected_hash=first.snapshot.content_hash,
        actor="human", reason="建立手册",
    )
    path = tmp_path / "memory" / scope.storage_key / "MEMORY.md"
    invalid = "超长内容" * 20
    path.write_text(invalid, encoding="utf-8")

    fallback = editor.read(scope)
    assert fallback.snapshot.content == good.content
    assert fallback.diagnostics[0].code == "memory_document_invalid"
    assert path.read_text(encoding="utf-8") == invalid
    with pytest.raises(MemoryManualFileInvalidError):
        editor.replace_user(
            scope, "# 修复", expected_hash=fallback.disk_hash,
            actor="model", reason="修复", source_ref="turn-2",
        )

    restored = editor.restore_user(
        scope, 2, expected_hash=fallback.disk_hash,
        actor="human", reason="恢复有效版本",
    )
    assert restored.content == good.content
    assert editor.read(scope).diagnostics == ()
    assert len(_versions(storage, scope)) == 3
    assert _versions(storage, scope)[-1][3].startswith("restore:2:")


def test_invalid_first_file_does_not_enter_context_or_history(tmp_path):
    editor, storage = _editor(tmp_path, max_tokens=5)
    scope = _scope()
    path = tmp_path / "memory" / scope.storage_key / "MEMORY.md"
    path.parent.mkdir(parents=True)
    path.write_text("太长了" * 10, encoding="utf-8")

    result = editor.read(scope)
    assert result.snapshot.content == ""
    assert result.diagnostics[0].code == "memory_document_invalid"
    assert _versions(storage, scope) == []


def test_peek_validates_without_registering_external_edit(tmp_path):
    editor, storage = _editor(tmp_path)
    scope = _scope()
    path = tmp_path / "memory" / scope.storage_key / "MEMORY.md"
    path.parent.mkdir(parents=True)
    path.write_text("# 手动内容\n", encoding="utf-8")
    preview = editor.read(scope, persist=False)
    assert preview.snapshot.content == "# 手动内容\n"
    assert preview.revision is None
    assert _versions(storage, scope) == []
    assert editor.read(scope).revision == 1


def test_invalid_utf8_file_can_be_restored_by_disk_hash(tmp_path):
    editor, storage = _editor(tmp_path)
    scope = _scope()
    initial = editor.read(scope)
    good = editor.replace_user(
        scope, "# 稳定信息\n", expected_hash=initial.disk_hash,
        actor="human", reason="建立手册",
    )
    path = tmp_path / "memory" / scope.storage_key / "MEMORY.md"
    path.write_bytes(b"\xff\xfe")

    fallback = editor.read(scope)
    assert fallback.snapshot.content == good.content
    assert fallback.diagnostics[0].code == "memory_document_invalid"
    restored = editor.restore_user(
        scope, 2, expected_hash=fallback.disk_hash,
        actor="human", reason="修复编码",
    )
    assert path.read_text(encoding="utf-8") == restored.content
    assert _versions(storage, scope)[-1][2] == "human"


def test_rejects_group_scope_secrets_and_budget_without_touching_policy(tmp_path):
    editor, _ = _editor(tmp_path, max_tokens=10)
    with pytest.raises(MemoryManualScopeError):
        editor.read(MemoryScope(tenant_id="group"))

    scope = _scope()
    initial_hash = editor.read(scope).snapshot.content_hash
    policy = tmp_path / "memory" / "POLICY.md"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("Policy content", encoding="utf-8")
    for content in ("密码: abcdef", "sk-abcdefghijklm", "中文" * 20):
        with pytest.raises(MemoryManualContentError):
            editor.replace_user(
                scope, content, expected_hash=initial_hash,
                actor="model", reason="错误输入", source_ref="turn-1",
            )
    assert policy.read_text(encoding="utf-8") == "Policy content"


def test_database_failure_restores_file_and_version(tmp_path, monkeypatch):
    from contextlib import contextmanager

    editor, storage = _editor(tmp_path)
    scope = _scope()
    initial = editor.read(scope)
    editor.replace_user(scope, "before", expected_hash=initial.disk_hash, actor="human", reason="initial")
    before = editor.read(scope)
    versions = _versions(storage, scope)
    real_write = storage.db.write

    @contextmanager
    def failed_commit():
        with real_write() as conn:
            yield conn
            raise RuntimeError("commit failed")

    with monkeypatch.context() as patch:
        patch.setattr(storage.db, "write", failed_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            editor.replace_user(scope, "after", expected_hash=before.disk_hash,
                                actor="model", reason="update", source_ref="message:m1")
    assert editor.read(scope).snapshot.content == "before"
    assert _versions(storage, scope) == versions
