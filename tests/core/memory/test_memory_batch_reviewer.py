from __future__ import annotations

from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_validator import MemoryValidator
from agent.core.memory.memory_batch_reviewer import MemoryBatchReviewer
from agent.types.memory.extraction import MemoryProposal, MemoryExtractionBatch
from agent.types.memory.source import TurnRecord
from agent.types.conversation import Message, Session


class _Extractor:
    def __init__(self, proposals):
        self._proposals = proposals

    def extract_batch(self, turns, sessions):
        return MemoryExtractionBatch(proposals=tuple(self._proposals))


def _proposal(proposal_id: str, message_id: str, *, content: str = "用户偏好中文",
              quote: str = "请用中文"):
    return MemoryProposal(
        proposal_id=proposal_id,
        kind="stable_preference",
        subject="assistant",
        attribute="response_language",
        summary=content,
        content=content,
        confidence=0.9,
        source_authority="user_explicit",
        source_message_ids=(message_id,),
        evidence_quotes={message_id: quote},
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
    reviewer = MemoryBatchReviewer(
        _Extractor([_proposal("proposal-1", "user-1")]),
        validator=MemoryValidator(id_factory=lambda: "memory-1"),
        id_factory=iter(("trace-item-1", "trace-1")).__next__,
        clock=iter((1.0, 1.01)).__next__,
    )

    result = reviewer.review_batch((_turn(scope),), (session,))

    assert [item.item_id for item in result.user_items] == ["memory-1"]
    assert result.trace.items[0].runtime.validation_codes == ("accepted",)
    assert result.trace.items[0].ai.reason_code == "explicit_preference"


def test_review_rejects_recalled_evidence() -> None:
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
    reviewer = MemoryBatchReviewer(
        _Extractor(
            [
                _proposal("proposal-1", "user-1"),
                _proposal("proposal-2", "user-1"),
            ]
        ),
        id_factory=iter(("trace-item-1", "trace-item-2", "trace-1")).__next__,
        clock=iter((1.0, 1.01)).__next__,
    )

    result = reviewer.review_batch((_turn(scope),), (session,))

    assert result.user_items == ()
    assert "untrusted_message_evidence" in result.validations[0].validation_codes
    assert "untrusted_message_evidence" in result.validations[1].validation_codes


def test_model_claim_requires_overlap_with_cited_source() -> None:
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    session = Session(session_id="session-1")
    session.add_message(Message(message_id="user-1", role="user", content_text="今天下雨了"))
    inferred = _proposal("proposal-1", "user-1", content="用户擅长量子物理",
                         quote="今天下雨了")
    inferred = inferred.model_copy(update={"source_authority": "model_inferred"})
    reviewer = MemoryBatchReviewer(
        _Extractor([inferred]),
        id_factory=iter(("trace-item-1", "trace-1")).__next__,
        clock=iter((1.0, 1.01)).__next__,
    )
    result = reviewer.review_batch((_turn(scope),), (session,))
    assert result.user_items == ()
    assert "claim_without_source_overlap" in result.validations[0].validation_codes


def test_one_source_can_yield_two_independent_supported_cards() -> None:
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    text = "我正在开发 DemoCursor，项目使用 Python 3.11。"
    session = Session(session_id="session-1")
    session.add_message(Message(message_id="user-1", role="user", content_text=text))
    cards = [MemoryProposal(
        proposal_id=f"p-{attribute}",
        kind="ongoing_fact", subject="DemoCursor", attribute=attribute,
        summary=claim, content=claim, confidence=0.8,
        source_authority="model_inferred", source_message_ids=("user-1",),
        evidence_quotes={"user-1": text}, reason_code="direct_project_fact",
    ) for attribute, claim in (
        ("current_project", "我正在开发 DemoCursor"),
        ("python_version", "DemoCursor 使用 Python 3.11"),
    )]
    result = MemoryBatchReviewer(_Extractor(cards)).review_batch(
        (_turn(scope),), (session,))
    assert len(result.user_items) == 2
    assert {card.slot_key for card in result.user_items} == {
        "ongoing_fact:democursor:current_project",
        "ongoing_fact:democursor:python_version",
    }
    assert all(card.source_authority == "user_explicit" for card in result.user_items)


def test_model_cannot_add_version_missing_from_exact_quote() -> None:
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    session = Session(session_id="session-1")
    session.add_message(Message(message_id="user-1", role="user",
                                content_text="我正在开发 DemoCursor。"))
    proposal = MemoryProposal(
        proposal_id="p-version",
        kind="ongoing_fact", subject="DemoCursor", attribute="python_version",
        summary="DemoCursor 使用 Python 3.11", content="DemoCursor 使用 Python 3.11",
        confidence=0.8, source_authority="model_inferred",
        source_message_ids=("user-1",),
        evidence_quotes={"user-1": "我正在开发 DemoCursor"},
        reason_code="unsupported_version",
    )
    result = MemoryBatchReviewer(_Extractor([proposal])).review_batch((_turn(scope),), (session,))
    assert result.user_items == ()
    assert "claim_anchor_missing_from_quote" in result.validations[0].validation_codes


def test_failed_experience_cannot_be_saved_as_success() -> None:
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    session = Session(session_id="session-1")
    session.add_message(Message(message_id="user-1", role="user",
                                content_text="pytest 运行失败了"))
    proposal = MemoryProposal(
        proposal_id="p-failure",
        kind="reusable_experience", summary="pytest 运行成功",
        content="这次 pytest 运行成功", keywords=("pytest", "Windows", "虚拟环境"),
        applicability="在 Windows 使用虚拟环境运行 pytest",
        action="运行 pytest", outcome="这次运行成功", limitations="单次观察",
        outcome_status="success", confidence=0.7,
        source_authority="model_inferred", source_message_ids=("user-1",),
        evidence_quotes={"user-1": "pytest 运行失败了"},
        reason_code="mistaken_outcome",
    )
    result = MemoryBatchReviewer(_Extractor([proposal])).review_batch((_turn(scope),), (session,))
    assert result.user_items == ()
    assert "experience_outcome_not_supported" in result.validations[0].validation_codes
