"""ModelBasedExtractor：固定 JSON → BuiltinMemoryRequest；失败降级 RuleBased。"""
from __future__ import annotations

import pytest

from Agent.core.types import Message
from Agent.memory import (
    MemoryReadRequest,
    MemoryManager,
    MemoryScope,
)
from Agent.memory.episodic.store import SqliteSessionStore
from Agent.memory.extractors import (
    FallbackMemoryExtractor,
    ModelBasedExtractor,
    ModelExtractionError,
    RuleBasedExtractor,
)
from Agent.memory.reviewer import TurnReviewer
from Agent.memory.types import TurnRecord
from Agent.model.baseStructure import ModelResponse

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")


class _ScriptModel:
    name = "script"

    def __init__(self, script):
        self._script = list(script)

    def chat(self, messages, tools=None, **kwargs):
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _commit_user(store, text: str, *, turn_id: str = "t1"):
    session = store.get_or_create("k", scope=_SCOPE)
    msg = Message(role="user", content_text=text)
    session.add_message(msg)
    turn = TurnRecord(
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


def test_model_extractor_applies_fixed_json_via_reviewer(tmp_path):
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    _commit_user(store, "请记住我喜欢先看结论")

    model = _ScriptModel([
        ModelResponse(
            text='[{"target":"profile","action":"add","content":"用户偏好先看结论"}]'
        )
    ])
    extractor = ModelBasedExtractor(store, model)
    assert TurnReviewer(store, manager, extractor).process_pending() == 1

    view = manager.read(MemoryReadRequest(scope=_SCOPE, target="profile"))
    assert any("先看结论" in e.content for e in view.entries)


def test_model_extractor_empty_array_writes_nothing(tmp_path):
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    _commit_user(store, "今天天气怎么样？")

    model = _ScriptModel([ModelResponse(text="[]")])
    extractor = ModelBasedExtractor(store, model)
    assert TurnReviewer(store, manager, extractor).process_pending() == 1

    profile = manager.read(MemoryReadRequest(scope=_SCOPE, target="profile"))
    assert profile.entries == ()


def test_fallback_uses_rule_when_model_fails(tmp_path):
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    _commit_user(store, "请记住我偏好简洁回答")

    boom = _ScriptModel([RuntimeError("api down")])
    extractor = FallbackMemoryExtractor(
        ModelBasedExtractor(store, boom),
        RuleBasedExtractor(store),
    )
    assert TurnReviewer(store, manager, extractor).process_pending() == 1

    view = manager.read(MemoryReadRequest(scope=_SCOPE, target="profile"))
    assert any("偏好简洁" in e.content for e in view.entries)


def test_fallback_does_not_downgrade_on_empty_json(tmp_path):
    """合法 [] 表示模型认为无记忆，不触发规则降级。"""
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    manager = MemoryManager(root=tmp_path / "memory")
    _commit_user(store, "请记住我偏好先给结论")

    model = _ScriptModel([ModelResponse(text="[]")])
    extractor = FallbackMemoryExtractor(
        ModelBasedExtractor(store, model),
        RuleBasedExtractor(store),
    )
    assert TurnReviewer(store, manager, extractor).process_pending() == 1

    view = manager.read(MemoryReadRequest(scope=_SCOPE, target="profile"))
    assert view.entries == ()


def test_model_bad_json_raises_extraction_error(tmp_path):
    db = tmp_path / "memory.db"
    store = SqliteSessionStore(db)
    session, turn = _commit_user(store, "请记住我叫小明")

    model = _ScriptModel([ModelResponse(text="不是json")])
    with pytest.raises(ModelExtractionError):
        ModelBasedExtractor(store, model).extract(turn)
