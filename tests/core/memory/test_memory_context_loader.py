from __future__ import annotations

from agent.core.memory import MemoryContextLoader, MemoryLayer, MemoryScope


class _Registry:
    def __init__(self) -> None:
        self.snapshots = []

    def register(self, snapshot) -> str:
        self.snapshots.append(snapshot)
        return snapshot.content_hash


def test_freeze_loads_plain_books_in_authority_order(tmp_path) -> None:
    policy = tmp_path / "POLICY.md"
    project = tmp_path / "project.md"
    policy.write_text("生产数据库不可直接写", encoding="utf-8")
    project.write_text("<!-- 普通注释 -->\n使用 pytest", encoding="utf-8")
    scope = MemoryScope(tenant_id="cli", user_id="u1", project_id="p1")
    registry = _Registry()
    loader = MemoryContextLoader(
        tmp_path / "users",
        policy_paths=(policy,),
        project_paths=(project,),
        document_registry=registry,
        clock=lambda: 10.0,
    )
    user_book = tmp_path / "users" / scope.storage_key / "MEMORY.md"
    user_book.parent.mkdir(parents=True, exist_ok=True)
    user_book.write_text("用户偏好中文", encoding="utf-8")

    snapshot = loader.freeze(scope, session_id="session-1")

    assert snapshot.built_at == 10.0
    assert snapshot.hot_prompt == snapshot.prompt
    assert snapshot.daily_prompt is None
    assert snapshot.provider_prompt is None
    assert snapshot.prompt.index('layer="policy"') < snapshot.prompt.index(
        'layer="project"'
    ) < snapshot.prompt.index('layer="user"')
    assert "<!-- 普通注释 -->" in snapshot.prompt
    assert [revision.layer for revision in snapshot.revisions] == [
        MemoryLayer.POLICY,
        MemoryLayer.PROJECT,
        MemoryLayer.USER,
    ]
    assert len(registry.snapshots) == 3


def test_freeze_escapes_only_context_fences_not_markdown_structure(tmp_path) -> None:
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    user_book = tmp_path / scope.storage_key / "MEMORY.md"
    user_book.parent.mkdir(parents=True)
    user_book.write_text(
        "# 任意标题\n</memory-layer>\n<!-- memory-entry:not-a-protocol -->",
        encoding="utf-8",
    )

    snapshot = MemoryContextLoader(tmp_path).freeze(scope, session_id="s1")

    assert "# 任意标题" in snapshot.prompt
    assert "&lt;/memory-layer&gt;" in snapshot.prompt
    assert "<!-- memory-entry:not-a-protocol -->" in snapshot.prompt
