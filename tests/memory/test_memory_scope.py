"""Slice 3: Personal/Group 隔离与路径安全——唯一 scope resolver + hash 目录。"""
from Agent.core.types import SessionSource
from Agent.memory import (
    BuiltinMemoryMutation,
    BuiltinMemoryReadRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
    build_memory_scope,
)

_PROVENANCE = MemoryProvenance(source="user_request")


def _add(manager: MemoryManager, scope: MemoryScope, content: str):
    return manager.apply(
        BuiltinMemoryMutation(
            scope=scope, target="profile", action="add",
            content=content, provenance=_PROVENANCE,
        )
    )


def test_personal_memory_is_not_visible_in_group_scope(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory")
    personal = build_memory_scope(
        SessionSource(channel="cli", chat_type="dm", user_id="u1", chat_id="c1")
    )
    group = build_memory_scope(
        SessionSource(channel="cli", chat_type="group", chat_id="g1")
    )
    _add(manager, personal, "个人偏好只给自己看")

    view = manager.read(BuiltinMemoryReadRequest(scope=group, target="profile"))

    assert "个人偏好只给自己看" not in view.rendered
    assert view.entries == ()


def test_two_channels_with_same_user_id_are_isolated(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory")
    cli = build_memory_scope(
        SessionSource(channel="cli", chat_type="dm", user_id="u1", chat_id="c1")
    )
    feishu = build_memory_scope(
        SessionSource(channel="feishu", chat_type="dm", user_id="u1", chat_id="c2")
    )
    _add(manager, cli, "只在 cli 记住我")

    view = manager.read(BuiltinMemoryReadRequest(scope=feishu, target="profile"))

    assert "只在 cli 记住我" not in view.rendered
    assert view.entries == ()


def test_group_memory_is_shared_by_members_of_same_group(tmp_path):
    manager = MemoryManager(root=tmp_path / "memory")
    member_a = build_memory_scope(
        SessionSource(channel="feishu", chat_type="group", chat_id="g1", user_id="ua")
    )
    member_b = build_memory_scope(
        SessionSource(channel="feishu", chat_type="group", chat_id="g1", user_id="ub")
    )
    added = _add(manager, member_a, "群共识：周五前交付")

    view = manager.read(BuiltinMemoryReadRequest(scope=member_b, target="profile"))

    assert [e.entry_id for e in view.entries] == [added.entry_id]
    assert "群共识：周五前交付" in view.rendered


def test_external_ids_cannot_escape_memory_root(tmp_path):
    root = tmp_path / "memory"
    manager = MemoryManager(root=root)
    evil = build_memory_scope(
        SessionSource(channel="cli", chat_type="dm", user_id="../../evil", chat_id="x")
    )
    added = _add(manager, evil, "恶意 ID 的偏好")
    assert added.ok

    # operator seam：root 下只允许出现 hash 目录，恶意片段不得物化为路径
    assert not (tmp_path / "evil").exists()
    for path in root.rglob("*"):
        assert path.resolve().is_relative_to(root.resolve())
        assert "../" not in str(path)
    # scope.json 保存可读标识供 operator 反查
    import json
    markers = [json.loads(p.read_text(encoding="utf-8")) for p in root.rglob("scope.json")]
    assert {"kind": "personal", "key": "cli:user:../../evil"} in markers


def test_group_profile_is_materialized_as_group_md(tmp_path):
    root = tmp_path / "memory"
    manager = MemoryManager(root=root)
    group = build_memory_scope(
        SessionSource(channel="feishu", chat_type="group", chat_id="g1")
    )
    _add(manager, group, "群偏好：中文交流")

    group_files = [p.name for p in root.rglob("GROUP.md")]
    user_files = [p.name for p in root.rglob("USER.md")]

    assert len(group_files) == 1
    assert user_files == []
    content = next(root.rglob("GROUP.md")).read_text(encoding="utf-8")
    assert content.startswith("# GROUP")
    assert "群偏好：中文交流" in content
