from __future__ import annotations

from agent.core.memory import MemoryLayer, MemoryScope, MemoryValidator, SessionMemoryReviewer
from agent.core.memory.types import MemoryProposal, TurnRecord
from agent.core.types import Message, Session


class _Extractor:
    def __init__(self, proposals):
        self._proposals = proposals

    def extract(self, turn, session):
        return list(self._proposals)


def _proposal(proposal_id: str, message_id: str, *, content: str = "用户偏好中文"):
    return MemoryProposal(
        proposal_id=proposal_id,
        target_layer=MemoryLayer.USER,
        type="preference",
        normalized_key="language.preference",
        summary=content,
        content=content,
        confidence=0.9,
        source_authority="user_explicit",
        source_message_ids=(message_id,),
        reason_code="explicit_preference",
    )


def _turn(scope, user_message_id="user-1"):
    return TurnRecord(
        turn_id="turn-1",
        session_id="session-1",
        scope=scope,
        user_message_id=user_message_id,
        assistant_message_ids=("assistant-1",),
        tool_call_ids=(),
        started_at=1.0,
        finished_at=2.0,
        completed=True,
        interrupted=False,
        error=None,
    )


def test_review_accepts_user_evidence_and_traces_decision() -> None:
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    session = Session(session_id="session-1")
    session.add_message(Message(message_id="user-1", role="user", content_text="请用中文"))
    session.add_message(Message(message_id="assistant-1", role="assistant", content_text="好的"))
    reviewer = SessionMemoryReviewer(
        _Extractor([_proposal("proposal-1", "user-1")]),
        validator=MemoryValidator(id_factory=lambda: "memory-1"),
        id_factory=iter(("trace-item-1", "trace-1")).__next__,
        clock=iter((1.0, 1.01)).__next__,
    )

    result = reviewer.review(_turn(scope), session)

    assert [item.item_id for item in result.user_items] == ["memory-1"]
    assert result.trace.items[0].runtime.validation_codes == ("accepted",)
    assert result.trace.items[0].ai.reason_code == "explicit_preference"


def test_review_rejects_recalled_or_duplicate_evidence() -> None:
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    session = Session(session_id="session-1")
    session.add_message(
        Message(
            message_id="user-1",
            role="user",
            content_text="旧记忆",
            metadata={"source_type": "memory_recalled"},
        )
    )
    reviewer = SessionMemoryReviewer(
        _Extractor(
            [
                _proposal("proposal-1", "user-1"),
                _proposal("proposal-2", "user-1"),
            ]
        ),
        id_factory=iter(("trace-item-1", "trace-item-2", "trace-1")).__next__,
        clock=iter((1.0, 1.01)).__next__,
    )

    result = reviewer.review(_turn(scope), session)

    assert result.user_items == ()
    assert "untrusted_message_evidence" in result.validations[0].validation_codes
    assert "duplicate_in_batch" in result.validations[1].validation_codes
