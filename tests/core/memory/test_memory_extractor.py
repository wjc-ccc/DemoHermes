from __future__ import annotations

from agent.core.memory import (
    FallbackMemoryExtractor,
    MemoryLayer,
    MemoryScope,
    ModelMemoryExtractor,
    RuleBasedMemoryExtractor,
)
from agent.core.memory.types import TurnRecord
from agent.core.types import Message, Session


def _turn(scope, message_id="m1"):
    return TurnRecord(
        turn_id="t1",
        session_id="s1",
        scope=scope,
        user_message_id=message_id,
        assistant_message_ids=(),
        tool_call_ids=(),
        started_at=1.0,
        finished_at=2.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def _session(text):
    session = Session(session_id="s1")
    session.add_message(Message(message_id="m1", role="user", content_text=text))
    return session


def test_rule_extractor_separates_user_preference_and_task_progress() -> None:
    ids = iter(("p-user", "p-task"))
    extractor = RuleBasedMemoryExtractor(id_factory=ids.__next__)
    user_scope = MemoryScope(user_id="u1")

    user = extractor.extract(_turn(user_scope), _session("以后请用中文回答"))
    task_scope = MemoryScope(user_id="u1", task_id="task-1")
    task = extractor.extract(_turn(task_scope), _session("已经完成了 SQLite 迁移"))

    assert user[0].target_layer is MemoryLayer.USER
    assert user[0].type == "preference"
    assert user[0].source_authority == "user_explicit"
    assert task[0].target_layer is MemoryLayer.TASK
    assert task[0].type == "progress"


def test_model_extractor_output_stays_proposed_authority() -> None:
    class _Response:
        text = """[{"target_layer":"user","type":"capability",
        "memory_kind":"semantic","normalized_key":"capability.python",
        "summary":"可能熟悉 Python","content":"用户可能熟悉 Python",
        "confidence":0.7,"reason_code":"inferred_capability"}]"""

    class _Model:
        def chat(self, messages):
            return _Response()

    proposal = ModelMemoryExtractor(
        _Model(), id_factory=lambda: "proposal-1"
    ).extract(_turn(MemoryScope(user_id="u1")), _session("我用 Python 写过脚本"))[0]

    assert proposal.source_authority == "model_inferred"
    assert proposal.target_layer is MemoryLayer.USER


def test_model_failure_falls_back_but_valid_empty_does_not() -> None:
    class _Broken:
        version = "broken"

        def extract(self, turn, session):
            from agent.core.memory import ModelMemoryExtractionError

            raise ModelMemoryExtractionError("bad")

    class _Empty:
        version = "empty"

        def extract(self, turn, session):
            return []

    fallback = RuleBasedMemoryExtractor(id_factory=lambda: "proposal-1")
    turn = _turn(MemoryScope(user_id="u1"))
    session = _session("以后请用中文回答")

    assert FallbackMemoryExtractor(_Broken(), fallback).extract(turn, session)
    assert FallbackMemoryExtractor(_Empty(), fallback).extract(turn, session) == []
