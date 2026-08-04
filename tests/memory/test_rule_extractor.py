"""RuleBasedExtractor：生产默认规则抽取（§8.2 允许规则化）。"""
from __future__ import annotations

from Agent.core.types import Message
from Agent.memory import (
    BuiltinMemoryReadRequest,
    MemoryManager,
    MemoryScope,
    MemorySearchRequest,
)
from Agent.memory.episodic.store import SqliteSessionStore
from Agent.memory.extractors import RuleBasedExtractor
from Agent.memory.reviewer import MemoryReviewer
from Agent.memory.types import TurnEnvelope

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")


def _commit_user(store, text: str, *, turn_id: str = "t1"):
    session = store.get_or_create("k", scope=_SCOPE)
    msg = Message(role="user", content_text=text)
    session.add_message(msg)
    turn = TurnEnvelope(
        turn_id=turn_id,
        session_id=session.session_id,
        scope=_SCOPE,
        user_message_id=msg.message_id,
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1000.0,
        finished_at=1001.0,
        completed=True,
        interrupted=False,
        error=None,
    )
    store.commit_turn(session, turn, scope=_SCOPE)
    return session, turn


def test_rule_extractor_writes_daily_for_event(tmp_path):
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    _commit_user(store, "今天把服务部署到了 staging 环境")

    reviewer = MemoryReviewer(store, manager, RuleBasedExtractor(store))
    assert reviewer.process_pending() == 1

    hits = manager.search(
        MemorySearchRequest(scope=_SCOPE, query="staging", sources=("daily",))
    )
    assert any("staging" in h.snippet for h in hits)


def test_rule_extractor_writes_profile_for_preference(tmp_path):
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    _commit_user(store, "请记住我偏好先给结论")

    reviewer = MemoryReviewer(store, manager, RuleBasedExtractor(store))
    assert reviewer.process_pending() == 1

    view = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    assert any("偏好先给结论" in e.content for e in view.entries)


def test_rule_extractor_skips_one_off_question(tmp_path):
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    _commit_user(store, "今天天气怎么样？")

    reviewer = MemoryReviewer(store, manager, RuleBasedExtractor(store))
    assert reviewer.process_pending() == 1

    profile = manager.read(BuiltinMemoryReadRequest(scope=_SCOPE, target="profile"))
    assert profile.entries == ()
    hits = manager.search(
        MemorySearchRequest(scope=_SCOPE, query="天气", sources=("daily",))
    )
    assert hits == []
