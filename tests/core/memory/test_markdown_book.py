from __future__ import annotations

import pytest

from agent.core.memory import (
    MarkdownBook,
    MarkdownBookReadOnlyError,
    MarkdownRevisionConflictError,
    MemoryLayer,
)


def test_markdown_book_reads_natural_markdown_without_markers(tmp_path) -> None:
    path = tmp_path / "MEMORY.md"
    content = "# 我的手册\n\n可以任意写段落，不需要隐藏 ID。\n"
    path.write_text(content, encoding="utf-8")
    book = MarkdownBook(
        path,
        layer=MemoryLayer.USER,
        type="long_term_fact",
        writable=True,
    )

    snapshot = book.read()

    assert snapshot.content == content
    assert snapshot.token_count > 0
    assert "memory-entry" not in snapshot.content


def test_markdown_book_replace_is_revision_checked(tmp_path) -> None:
    path = tmp_path / "MEMORY.md"
    book = MarkdownBook(
        path,
        layer=MemoryLayer.USER,
        type="long_term_fact",
        writable=True,
    )
    initial = book.replace("第一版")
    changed = book.replace("第二版", expected_hash=initial.content_hash)
    assert changed.content == "第二版"

    with pytest.raises(MarkdownRevisionConflictError):
        book.replace("过期写入", expected_hash=initial.content_hash)


def test_policy_book_is_read_only_by_default(tmp_path) -> None:
    book = MarkdownBook(
        tmp_path / "POLICY.md",
        layer=MemoryLayer.POLICY,
        type="security_boundary",
    )
    with pytest.raises(MarkdownBookReadOnlyError):
        book.replace("不能直接写生产数据库")
