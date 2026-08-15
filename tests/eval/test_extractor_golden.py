"""E2：记忆抽取 golden 集（8.15）——对 RuleBasedExtractor.extract 公开面回归。

期望值为字面量 target 序列（含空 = 不写）。不 mock Store/Extractor。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Agent.core.types import Message
from Agent.memory import MemoryScope
from Agent.memory.episodic.store import SqliteSessionStore
from Agent.memory.extractors import RuleBasedExtractor
from Agent.memory.types import TurnRecord

_SCOPE = MemoryScope(kind="personal", key="cli:user:eval-extract")

# (id, user_text, expected_targets) — expected 为空元组表示不写任何层
_GOLDENS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("greet_nihao", "你好", ()),
    ("greet_hello", "hello", ()),
    ("slash_help", "/help", ()),
    ("oneoff_weather", "今天天气怎么样？", ()),
    ("oneoff_short_q", "几点了？", ()),
    ("pref_remember", "请记住我偏好先给结论", ("profile",)),
    ("pref_like", "我喜欢用简体中文交流", ("profile",)),
    ("agent_must", "你必须总是先列步骤再写代码", ("memory",)),
    ("agent_always", "请始终用中文回答", ("memory",)),
    ("daily_deploy", "今天把服务部署到了 staging 环境", ("daily",)),
    ("daily_meeting", "下午三点和产品对齐了需求范围", ("daily",)),
)


def _commit_user(store: SqliteSessionStore, text: str, *, turn_id: str) -> TurnRecord:
    session = store.get_or_create(f"k-{turn_id}", scope=_SCOPE)
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
    return turn


@pytest.mark.parametrize(
    "case_id,user_text,expected_targets",
    _GOLDENS,
    ids=[g[0] for g in _GOLDENS],
)
def test_rule_extractor_golden_targets(tmp_path, case_id, user_text, expected_targets):
    store = SqliteSessionStore(tmp_path / f"{case_id}.db")
    turn = _commit_user(store, user_text, turn_id=case_id)
    extractor = RuleBasedExtractor(store)

    reqs = extractor.extract(turn)
    targets = tuple(r.target for r in reqs)

    assert targets == expected_targets
