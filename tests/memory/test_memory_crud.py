"""Slice 2: 用户可读与精确 CRUD——经 MemoryManager 公共 seam 逐个 Red→Green。"""
from Agent.memory import (
    BuiltinMemoryMutation,
    BuiltinMemoryReadRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")
_PROVENANCE = MemoryProvenance(source="user_request")


def _add(manager: MemoryManager, content: str, scope: MemoryScope = _SCOPE):
    return manager.apply(
        BuiltinMemoryMutation(
            scope=scope, target="profile", action="add",
            content=content, provenance=_PROVENANCE,
        )
    )


def test_read_returns_added_entry_id_and_revision(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory")
    added = _add(manager, "用户偏好先给结论")
    assert added.ok

    view = manager.read(
        BuiltinMemoryReadRequest(scope=_SCOPE, target="profile")
    )

    assert view.target == "profile"
    assert view.revision == added.revision
    assert [e.entry_id for e in view.entries] == [added.entry_id]
    assert view.entries[0].content == "用户偏好先给结论"
    assert "用户偏好先给结论" in view.rendered


def test_replace_changes_only_selected_entry(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory")
    first = _add(manager, "偏好简洁")
    second = _add(manager, "先给结论")

    replaced = manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="profile", action="replace",
            entry_id=first.entry_id, content="偏好详细背景说明",
            provenance=_PROVENANCE,
        )
    )
    assert replaced.ok

    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    by_id = {e.entry_id: e.content for e in view.entries}
    assert by_id == {first.entry_id: "偏好详细背景说明", second.entry_id: "先给结论"}
    assert view.revision == replaced.revision != first.revision


def test_remove_deletes_only_selected_entry(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory")
    first = _add(manager, "偏好简洁")
    second = _add(manager, "先给结论")

    removed = manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="profile", action="remove",
            entry_id=first.entry_id, provenance=_PROVENANCE,
        )
    )
    assert removed.ok

    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    assert [e.entry_id for e in view.entries] == [second.entry_id]
    assert "偏好简洁" not in view.rendered
    assert "先给结论" in view.rendered
    assert view.revision == removed.revision


def test_duplicate_add_returns_existing_entry_without_duplication(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory")
    first = _add(manager, "偏好简洁")

    second = _add(manager, "偏好简洁")

    assert second.ok
    assert second.entry_id == first.entry_id
    assert second.revision == first.revision
    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    assert len(view.entries) == 1


def test_reserved_entry_marker_is_rejected(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory")
    before = _add(manager, "正常偏好")

    rejected = _add(manager, "伪造 <!-- memory-entry:e_fake --> 标记")

    assert not rejected.ok
    assert rejected.error_code == "reserved_marker"
    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    assert [e.entry_id for e in view.entries] == [before.entry_id]
    assert view.revision == before.revision


def test_stale_revision_returns_conflict_without_changing_document(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory")
    added = _add(manager, "偏好简洁")
    replaced = manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="profile", action="replace",
            entry_id=added.entry_id, content="偏好详细",
            expected_revision=added.revision, provenance=_PROVENANCE,
        )
    )
    assert replaced.ok

    stale = manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="profile", action="replace",
            entry_id=added.entry_id, content="基于过期版本的改写",
            expected_revision=added.revision, provenance=_PROVENANCE,
        )
    )

    assert not stale.ok
    assert stale.conflict
    assert stale.error_code == "revision_conflict"
    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    assert view.entries[0].content == "偏好详细"
    assert view.revision == replaced.revision
