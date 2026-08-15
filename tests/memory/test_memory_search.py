"""Slice 11: 历史检索是 scope-safe 且渐进的（§3.6/§4.2/§6.4）。

公共 seam：MemoryManager.search（内部使用真实 SQLite archive reader）。
不 mock sqlite3，不 SELECT 表，不断言任意实现分数。
"""
import pytest
from pydantic import ValidationError

from Agent.core.types import Message
from Agent.memory import (
    BuiltinMemoryRequest,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
    MemorySearchRequest,
)
from Agent.memory.episodic.store import SqliteSessionArchiveReader, SqliteSessionStore
from Agent.memory.types import SessionMessageRef, TurnRecord

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")
_OTHER_SCOPE = MemoryScope(kind="personal", key="cli:user:u2")
_PROVENANCE = MemoryProvenance(source="reviewer", turn_id="t_001")


def _turn(session, *, turn_id="t1", scope=_SCOPE) -> TurnRecord:
    return TurnRecord(
        turn_id=turn_id,
        session_id=session.session_id,
        scope=scope,
        user_message_id=session.messages[-1].message_id,
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def _setup(tmp_path, scope=_SCOPE, key="k", texts=("上周部署到 staging 的配置", "无关回复")):
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    session = store.get_or_create(key, scope=scope)
    for text in texts:
        session.add_message(Message(role="user", content_text=text))
    store.commit_turn(session, _turn(session), scope=scope)
    manager = MemoryManager(
        root=tmp_path / "memory",
        archive_reader=SqliteSessionArchiveReader(db),
    )
    return session, manager


def test_history_search_finds_unique_message_text(tmp_path):
    """session source：命中唯一消息文本，返回 typed ref（不含 SQL/路径）。"""
    session, manager = _setup(tmp_path)

    hits = manager.search(
        MemorySearchRequest(scope=_SCOPE, query="staging", sources=("session",))
    )

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source == "session"
    assert "staging" in hit.snippet
    assert hit.ref == SessionMessageRef(
        session_id=session.session_id, message_id=session.messages[0].message_id
    )


def test_history_search_never_returns_other_scope(tmp_path):
    """session source：只返回当前 scope 的命中，另一 scope 的同关键词消息不泄漏（§6.4）。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    s1 = store.get_or_create("k1", scope=_SCOPE)
    s1.add_message(Message(role="user", content_text="u1 的 staging 部署记录"))
    store.commit_turn(s1, _turn(s1, turn_id="t1"), scope=_SCOPE)

    s2 = store.get_or_create("k2", scope=_OTHER_SCOPE)
    s2.add_message(Message(role="user", content_text="u2 的 staging 部署记录"))
    store.commit_turn(s2, _turn(s2, turn_id="t2", scope=_OTHER_SCOPE), scope=_OTHER_SCOPE)

    manager = MemoryManager(
        root=tmp_path / "memory", archive_reader=SqliteSessionArchiveReader(db)
    )

    hits = manager.search(
        MemorySearchRequest(scope=_SCOPE, query="staging", sources=("session",))
    )

    assert len(hits) == 1
    assert hits[0].ref == SessionMessageRef(
        session_id=s1.session_id, message_id=s1.messages[0].message_id
    )


def test_search_all_merges_daily_and_session_hits(tmp_path):
    """sources=all 合并 daily 与 session 命中，按 §4.2 统一排序（§3.6）。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="讨论了 staging 环境的部署"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    manager = MemoryManager(
        root=tmp_path / "memory", archive_reader=SqliteSessionArchiveReader(db)
    )
    manager.apply(
        BuiltinMemoryRequest(
            scope=_SCOPE, target="daily", action="add", date="2026-07-23",
            content="staging 环境出现告警", provenance=_PROVENANCE,
        )
    )

    hits = manager.search(MemorySearchRequest(scope=_SCOPE, query="staging"))

    assert {h.source for h in hits} == {"daily", "session"}


def test_exact_match_ranks_before_partial_match(tmp_path):
    """§4.2：整串相等(exact)相关性高于子串命中(partial)。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="staging 环境部署细节"))  # partial
    session.add_message(Message(role="user", content_text="staging"))  # exact
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    manager = MemoryManager(
        root=tmp_path / "memory", archive_reader=SqliteSessionArchiveReader(db)
    )

    hits = manager.search(
        MemorySearchRequest(scope=_SCOPE, query="staging", sources=("session",))
    )

    assert len(hits) == 2
    assert hits[0].ref.message_id == session.messages[1].message_id  # exact 排前
    assert hits[1].ref.message_id == session.messages[0].message_id  # partial 靠后


def test_search_respects_result_and_snippet_limits(tmp_path):
    """§3.6/§4.2：limit 截断结果数；snippet_max_chars 截断片段长度。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    session = store.get_or_create("k", scope=_SCOPE)
    for i in range(5):
        session.add_message(Message(role="user", content_text=f"staging 记录 {i}"))
    session.add_message(Message(role="user", content_text="前" * 60 + "staging" + "后" * 60))
    long_id = session.messages[-1].message_id
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    manager = MemoryManager(
        root=tmp_path / "memory", archive_reader=SqliteSessionArchiveReader(db)
    )

    # limit 截断：6 条命中，limit=3 只返回 3
    hits_limited = manager.search(
        MemorySearchRequest(scope=_SCOPE, query="staging", sources=("session",), limit=3)
    )
    assert len(hits_limited) == 3

    # snippet 截断：长消息片段不超过 snippet_max_chars
    hits_snip = manager.search(
        MemorySearchRequest(
            scope=_SCOPE, query="staging", sources=("session",),
            limit=10, snippet_max_chars=50,
        )
    )
    long_hit = next(h for h in hits_snip if h.ref.message_id == long_id)
    assert len(long_hit.snippet) <= 50


def test_empty_query_is_rejected():
    """§3.6：空白 query 在进入 adapter 前拒绝（model_validator），纯空白同样拒绝。"""
    with pytest.raises(ValidationError):
        MemorySearchRequest(scope=_SCOPE, query="", sources=("session",))
    with pytest.raises(ValidationError):
        MemorySearchRequest(scope=_SCOPE, query="   ", sources=("session",))


def test_percent_underscore_and_escape_are_searched_as_literals(tmp_path):
    """§6.4：query 中的 %、_、\\ 按字面量搜索，不充当 LIKE 通配符。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(
        Message(role="user", content_text="完成 50% 的 task_1，路径 C:\\Users")
    )  # 含 % _ \
    session.add_message(Message(role="user", content_text="完全无关的另一条消息"))  # 无 % _ \
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    manager = MemoryManager(
        root=tmp_path / "memory", archive_reader=SqliteSessionArchiveReader(db)
    )

    def _count(query: str) -> int:
        return len(
            manager.search(
                MemorySearchRequest(scope=_SCOPE, query=query, sources=("session",))
            )
        )

    assert _count("50%") == 1  # % 字面量
    assert _count("task_1") == 1  # _ 字面量
    assert _count("C:\\Users") == 1  # \ 字面量
    assert _count("%") == 1  # 单独 % 不当通配符，只命中含字面量 % 的消息


def test_casefolded_search_is_independent_of_sqlite_default_collation(tmp_path):
    """§6.4：大小写由 py_casefold 锁定为 Unicode casefold，不依赖 SQLite build 默认 collation。

    SQLite 默认 LIKE 仅对 ASCII 大小写不敏感；用非 ASCII 大小写差异证明匹配走的是
    Python casefold（ÉTAGE ↔ étage），而非 SQLite 默认 collation。
    """
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    session = store.get_or_create("k", scope=_SCOPE)
    session.add_message(Message(role="user", content_text="ÉTAGE 部署完成"))
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    manager = MemoryManager(
        root=tmp_path / "memory", archive_reader=SqliteSessionArchiveReader(db)
    )

    hits = manager.search(
        MemorySearchRequest(scope=_SCOPE, query="étage", sources=("session",))
    )

    assert len(hits) == 1
    assert hits[0].ref.message_id == session.messages[0].message_id


def test_equal_scores_use_time_then_stable_ref_order(tmp_path):
    """§4.2：同分按 occurred_at DESC，再按 typed ref 稳定字符串升序。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    session = store.get_or_create("k", scope=_SCOPE)
    # 同分（均 partial=1.0）；created_at 与 message_id 全部可控
    session.add_message(
        Message(role="user", content_text="staging 部署 A", message_id="m_a", created_at=1000.0)
    )
    session.add_message(
        Message(role="user", content_text="staging 部署 B", message_id="m_b", created_at=3000.0)
    )
    session.add_message(
        Message(role="user", content_text="staging 部署 C", message_id="m_c", created_at=2000.0)
    )
    session.add_message(
        Message(role="user", content_text="staging 部署 D", message_id="m_d", created_at=2000.0)
    )
    store.commit_turn(session, _turn(session), scope=_SCOPE)

    manager = MemoryManager(
        root=tmp_path / "memory", archive_reader=SqliteSessionArchiveReader(db)
    )

    hits = manager.search(
        MemorySearchRequest(scope=_SCOPE, query="staging", sources=("session",), limit=10)
    )

    # m_b(最新) -> m_c,m_d(同时间，ref 升序) -> m_a(最旧)
    assert [h.ref.message_id for h in hits] == ["m_b", "m_c", "m_d", "m_a"]
