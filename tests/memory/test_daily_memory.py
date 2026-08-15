"""Slice 6: Daily 是按日可读日志，不默认污染 Context（§3.4 / §4.1 / §4.2）。"""
from Agent.memory import (
    BuiltinMemoryRequest,
    MemoryReadRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
    MemorySearchRequest,
)

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")
_PROVENANCE = MemoryProvenance(source="reviewer", turn_id="t_001")


def _scope_dir(root):
    return next(p for p in root.iterdir() if p.is_dir())


def test_daily_add_is_written_to_requested_date(tmp_path):
    root = tmp_path / "memory"
    manager = MemoryManager(root=root)
    result = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add",
            content="今天完成了 memory 方案评审", date="2026-07-23",
            provenance=_PROVENANCE,
        )
    )

    assert result.ok, result.message
    assert result.entry_id is not None
    # operator seam：daily/YYYY-MM-DD.md 落盘且含内容与 entry 围栏
    daily_path = _scope_dir(root) / "daily" / "2026-07-23.md"
    text = daily_path.read_text(encoding="utf-8")
    assert "今天完成了 memory 方案评审" in text
    assert f"<!-- memory-entry:{result.entry_id} -->" in text
    # read seam 按日期读回同一条 entry
    view = manager.read(
        MemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-23")
    )
    assert [e.entry_id for e in view.entries] == [result.entry_id]
    assert view.revision == result.revision


def test_daily_replace_does_not_change_other_date(tmp_path):
    root = tmp_path / "memory"
    manager = MemoryManager(root=root)
    first = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add",
            content="周三：方案评审", date="2026-07-23", provenance=_PROVENANCE,
        )
    )
    other = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add",
            content="周四：开始 TDD 开发", date="2026-07-24", provenance=_PROVENANCE,
        )
    )
    assert first.ok and other.ok
    before = manager.read(
        MemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-24")
    )

    replaced = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="replace",
            entry_id=first.entry_id, content="周三：方案评审通过",
            date="2026-07-23", provenance=_PROVENANCE,
        )
    )

    assert replaced.ok, replaced.message
    day_view = manager.read(
        MemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-23")
    )
    assert [e.content for e in day_view.entries] == ["周三：方案评审通过"]
    # 另一日期的文档 revision 与内容完全不变
    after = manager.read(
        MemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-24")
    )
    assert after.revision == before.revision
    assert [e.content for e in after.entries] == ["周四：开始 TDD 开发"]


def test_daily_is_absent_from_default_context(tmp_path):
    """§4.1 / ADR-4：Daily 默认不占每轮 prompt，需要时由 search/read 渐进调用。"""
    manager = MemoryManager(root=tmp_path / "memory")
    added = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add",
            content="今天评审了网关代码", date="2026-07-23", provenance=_PROVENANCE,
        )
    )
    assert added.ok

    snapshot = manager.build_context(scope=_SCOPE, query="继续", session_id="s1")

    assert "今天评审了网关代码" not in snapshot.prompt
    assert "daily" not in snapshot.prompt


_FIXED_NOON = 1784808000.0  # 2026-07-23 12:00:00 UTC


def test_today_daily_can_be_explicitly_included(tmp_path):
    """§4.1：include_today_daily=True 时注入今日 Daily；今日由注入 Clock 决定。"""
    manager = MemoryManager(root=tmp_path / "memory", clock=lambda: _FIXED_NOON)
    added = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add",
            content="今天中午锁定了 Slice 6 顺序",  # 不带 date：由 Clock 决定落哪天
            provenance=_PROVENANCE,
        )
    )
    assert added.ok
    # Clock 固定在 2026-07-23 UTC，无 date 的 add 落在当天文件
    day_view = manager.read(
        MemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-23")
    )
    assert [e.content for e in day_view.entries] == ["今天中午锁定了 Slice 6 顺序"]

    snapshot = manager.build_context(
        scope=_SCOPE, query="下午做什么", session_id="s1", include_today_daily=True
    )

    assert "今天中午锁定了 Slice 6 顺序" in snapshot.prompt


def _sequential_ids():
    counter = iter(range(1, 100))
    return lambda: f"e_{next(counter):012d}"


def test_daily_search_returns_entry_and_date_reference(tmp_path):
    """§3.6：daily 搜索命中返回 typed ref（date + entry_id），不返回路径/SQL。"""
    manager = MemoryManager(root=tmp_path / "memory", id_factory=_sequential_ids())
    wed = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add",
            content="周三喝了手冲咖啡", date="2026-07-23", provenance=_PROVENANCE,
        )
    )
    thu = manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add",
            content="周四咖啡配进度复盘", date="2026-07-24", provenance=_PROVENANCE,
        )
    )
    manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add",
            content="周四只写了文档", date="2026-07-24", provenance=_PROVENANCE,
        )
    )

    hits = manager.search(
        MemorySearchRequest(scope=_SCOPE, query="咖啡", sources=("daily",))
    )

    assert {(h.ref.date, h.ref.entry_id) for h in hits} == {
        ("2026-07-23", wed.entry_id),
        ("2026-07-24", thu.entry_id),
    }
    for hit in hits:
        assert hit.source == "daily"
        assert "咖啡" in hit.snippet


_BOUNDARY = 1784824200.0  # 2026-07-23 16:30:00 UTC == 2026-07-24 00:30:00 Asia/Shanghai


def test_daily_date_uses_configured_timezone_across_utc_midnight(tmp_path):
    """§4.2：日切以配置时区的自然日为准，不依赖 UTC 或操作系统隐式时区。

    Clock 固定在 UTC 2026-07-23 16:30：UTC 视角仍是 23 日，
    Asia/Shanghai 视角已跨过午夜进入 24 日。
    """
    from zoneinfo import ZoneInfo

    root = tmp_path / "memory"
    shanghai = MemoryManager(
        root=root, clock=lambda: _BOUNDARY, timezone=ZoneInfo("Asia/Shanghai")
    )
    utc = MemoryManager(
        root=root, clock=lambda: _BOUNDARY, timezone=ZoneInfo("UTC")
    )
    for mgr, content in ((shanghai, "跨午夜后的第一条"), (utc, "UTC 当天最后一条")):
        result = mgr.apply(
            BuiltinMemoryRequest(
                scope=_SCOPE, target="daily", action="add",
                content=content, provenance=_PROVENANCE,  # 不带 date：Clock+时区决定
            )
        )
        assert result.ok, result.message

    # 同一时刻落在两个不同自然日文件
    day23 = utc.read(MemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-23"))
    day24 = utc.read(MemoryReadRequest(scope=_SCOPE, target="daily", date="2026-07-24"))
    assert [e.content for e in day23.entries] == ["UTC 当天最后一条"]
    assert [e.content for e in day24.entries] == ["跨午夜后的第一条"]
    # Shanghai manager 的“今日”是 24 日
    snapshot = shanghai.build_context(
        scope=_SCOPE, query="早", session_id="s1", include_today_daily=True
    )
    assert "跨午夜后的第一条" in snapshot.prompt
    assert "UTC 当天最后一条" not in snapshot.prompt
