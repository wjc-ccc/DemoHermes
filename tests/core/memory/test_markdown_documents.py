"""统一文档入口的读取、并发写入和路径权限。"""
import pytest

from agent.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.types.memory.document import MarkdownRevisionConflictError, MemoryManualScopeError
from agent.types.memory.layers import MemoryLayer
from agent.types.memory.scope import MemoryScope


def test_reads_plain_markdown_and_normalizes_newlines(tmp_path):
    scope = MemoryScope(user_id="u1")
    path = tmp_path / scope.storage_key / "MEMORY.md"
    path.parent.mkdir()
    path.write_bytes("# 随意标题\r\n<!-- 普通注释 -->\r\n正文".encode("utf-8"))
    current = MarkdownDocuments(tmp_path).read(scope)
    assert current.snapshot.content == "# 随意标题\n<!-- 普通注释 -->\n正文"
    assert current.disk_hash == current.snapshot.content_hash


def test_user_replace_checks_observed_hash(tmp_path):
    scope = MemoryScope(user_id="u1")
    documents = MarkdownDocuments(tmp_path)
    first = documents.read(scope)
    changed = documents.replace_user(scope, "第一版", expected_hash=first.disk_hash, actor="human", reason="test")
    assert documents.read(scope).snapshot.content_hash == changed.content_hash
    with pytest.raises(MarkdownRevisionConflictError):
        documents.replace_user(scope, "第二版", expected_hash=first.disk_hash, actor="human", reason="stale")


def test_only_configured_documents_and_own_user_paths_can_be_read(tmp_path):
    scope = MemoryScope(user_id="u1")
    other = MemoryScope(user_id="u2")
    policy = tmp_path / "POLICY.md"
    policy.write_text("固定规则", encoding="utf-8")
    documents = MarkdownDocuments(tmp_path, policy_paths=(policy,))
    assert documents.read(scope, MemoryLayer.POLICY, policy).snapshot.content == "固定规则"
    for layer, path in ((MemoryLayer.PROJECT, policy), (MemoryLayer.USER, policy),
                        (MemoryLayer.USER, tmp_path / other.storage_key / "MEMORY.md")):
        with pytest.raises(MemoryManualScopeError):
            documents.read(scope, layer, path)
    with pytest.raises(MemoryManualScopeError):
        documents.replace_user(MemoryScope(tenant_id="group"), "bad", expected_hash="hash", actor="model", reason="bad", source_ref="m1")
    assert policy.read_text(encoding="utf-8") == "固定规则"
