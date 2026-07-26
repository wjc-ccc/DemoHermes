"""Slice 4: 并发不会丢更新——真实线程 + Barrier，不 mock 锁。"""
import threading

from Agent.memory import (
    BuiltinMemoryMutation,
    BuiltinMemoryReadRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")
_PROVENANCE = MemoryProvenance(source="user_request")

_ROUNDS = 25


def test_two_concurrent_adds_from_two_managers_are_both_preserved(tmp_path):
    root = tmp_path / "memory"
    manager_a = MemoryManager(root=root)
    manager_b = MemoryManager(root=root)  # 两个 Manager 实例共享同一 root

    # 每轮一个独立 scope：单文档 2 条 entry 远低于 1375 字符硬上限（Slice 5 预算），
    # 同时保留“同一文件被两线程并发 add”的完整竞争窗口。
    for round_no in range(_ROUNDS):
        scope = MemoryScope(kind="personal", key=f"cli:user:u{round_no}")
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def add(manager: MemoryManager, content: str) -> None:
            try:
                barrier.wait(timeout=10)
                result = manager.apply(
                    BuiltinMemoryMutation(
                        scope=scope, target="profile", action="add",
                        content=content, provenance=_PROVENANCE,
                    )
                )
                assert result.ok, result.message
            except Exception as exc:  # noqa: BLE001 - 收集线程内任何失败
                errors.append(exc)

        t1 = threading.Thread(target=add, args=(manager_a, f"甲线程第{round_no}条"))
        t2 = threading.Thread(target=add, args=(manager_b, f"乙线程第{round_no}条"))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert not errors

        view = manager_a.read(BuiltinMemoryReadRequest(scope=scope, target="profile"))
        assert {e.content for e in view.entries} == {
            f"甲线程第{round_no}条", f"乙线程第{round_no}条"
        }


def test_concurrent_replace_with_stale_revision_reports_one_conflict(tmp_path):
    root = tmp_path / "memory"
    manager_a = MemoryManager(root=root)
    manager_b = MemoryManager(root=root)
    added = manager_a.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="profile", action="add",
            content="原始偏好", provenance=_PROVENANCE,
        )
    )

    barrier = threading.Barrier(2)
    results = []

    def replace(manager: MemoryManager, content: str) -> None:
        barrier.wait(timeout=10)
        results.append(manager.apply(
            BuiltinMemoryMutation(
                scope=_SCOPE, target="profile", action="replace",
                entry_id=added.entry_id, content=content,
                expected_revision=added.revision, provenance=_PROVENANCE,
            )
        ))

    t1 = threading.Thread(target=replace, args=(manager_a, "甲的改写"))
    t2 = threading.Thread(target=replace, args=(manager_b, "乙的改写"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(r.ok for r in results) == [False, True]
    loser = next(r for r in results if not r.ok)
    assert loser.conflict and loser.error_code == "revision_conflict"
    view = manager_a.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    assert view.entries[0].content in ("甲的改写", "乙的改写")


def test_reader_never_observes_partial_markdown_during_replace(tmp_path):
    root = tmp_path / "memory"
    manager = MemoryManager(root=root)
    manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="profile", action="add",
            content="第0条偏好", provenance=_PROVENANCE,
        )
    )
    target_file = next(root.rglob("USER.md"))  # operator seam：直接读正式文件

    stop = threading.Event()
    bad_observations: list[str] = []
    writer_errors: list[Exception] = []

    def writer() -> None:
        try:
            for n in range(1, 200):
                manager.apply(
                    BuiltinMemoryMutation(
                        scope=_SCOPE, target="profile", action="add",
                        content=f"第{n}条偏好", provenance=_PROVENANCE,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 收集线程内任何失败
            writer_errors.append(exc)
        finally:
            stop.set()  # 无论成败都放行 reader，避免测试挂死

    def reader() -> None:
        while not stop.is_set():
            try:
                text = target_file.read_text(encoding="utf-8")
            except (PermissionError, FileNotFoundError):
                continue  # Windows 下 replace 窗口的瞬时共享冲突，非内容观测
            opens = text.count("<!-- memory-entry:")
            closes = text.count("<!-- /memory-entry:")
            if not text.startswith("# USER") or opens != closes or not text.endswith("\n"):
                bad_observations.append(text)

    t_w = threading.Thread(target=writer)
    t_r = threading.Thread(target=reader)
    t_r.start(); t_w.start(); t_w.join(); t_r.join()

    assert writer_errors == []
    assert bad_observations == []
