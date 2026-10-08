from __future__ import annotations

import pytest

from agent.core.memory import (
    MemoryScope, MemoryValidator, UserCardKind, UserCardStatus,
)
from agent.core.memory.types import MemoryProposal


def _proposal(**updates) -> MemoryProposal:
    values = {
        "proposal_id": "proposal-1",
        "kind": "stable_preference",
        "subject": "assistant", "attribute": "response_language",
        "summary": "用户偏好中文",
        "content": "用户偏好中文回答",
        "keywords": ("中文",),
        "confidence": 0.9,
        "source_authority": "user_explicit",
        "source_message_ids": ("message-1",),
        "evidence_quotes": {"message-1": "用户偏好中文回答"},
        "reason_code": "explicit_preference",
    }
    values.update(updates)
    return MemoryProposal(**values)


def test_explicit_user_fact_becomes_active_item() -> None:
    validator = MemoryValidator(id_factory=lambda: "memory-1")
    result = validator.validate(
        _proposal(), scope=MemoryScope(tenant_id="cli", user_id="u1")
    )
    assert result.accepted
    assert result.user_item is not None
    assert result.user_item.status is UserCardStatus.ACTIVE
    assert result.user_item.keywords == ("中文",)


def test_validated_inference_is_active_with_provenance() -> None:
    result = MemoryValidator(id_factory=lambda: "memory-1").validate(
        _proposal(source_authority="model_inferred"),
        scope=MemoryScope(tenant_id="cli", user_id="u1"),
    )
    assert result.user_item is not None
    assert result.user_item.status is UserCardStatus.ACTIVE
    assert result.user_item.source_authority == "model_inferred"
    assert result.user_item.kind is UserCardKind.STABLE_PREFERENCE


def test_experience_requires_specific_keywords_and_observation_fields() -> None:
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    proposal = _proposal(
        kind="reusable_experience",
        subject=None, attribute=None,
        summary="项目测试需使用虚拟环境",
        content="这次项目测试在 Windows 下使用虚拟环境后通过。",
        keywords=("pytest", "Windows", "虚拟环境"),
        applicability="这个项目在 Windows 下运行 pytest",
        action="激活项目虚拟环境后运行测试",
        outcome="本次测试通过",
        outcome_status="success",
        limitations="只在当前项目验证过",
    )
    valid = MemoryValidator().validate(proposal, scope=scope)
    assert valid.accepted
    assert valid.user_item.kind is UserCardKind.REUSABLE_EXPERIENCE
    assert valid.user_item.applicability == proposal.applicability

    failure = MemoryValidator().validate(proposal.model_copy(update={
        "outcome": "本次测试失败", "outcome_status": "failure",
    }), scope=scope)
    assert failure.accepted and failure.user_item.outcome_status.value == "failure"

    generic = MemoryValidator().validate(
        proposal.model_copy(update={"keywords": ("工作", "问题", "帮助")}),
        scope=scope,
    )
    assert not generic.accepted
    assert "invalid_experience_keywords" in generic.validation_codes

    incomplete = MemoryValidator().validate(
        proposal.model_copy(update={"limitations": None}), scope=scope,
    )
    assert not incomplete.accepted
    assert "incomplete_experience" in incomplete.validation_codes


def test_user_card_has_one_classification() -> None:
    item = MemoryValidator().validate(
        _proposal(), scope=MemoryScope(tenant_id="cli", user_id="u1")
    ).user_item
    assert item is not None
    fields = item.model_dump()
    assert fields["kind"] == UserCardKind.STABLE_PREFERENCE
    assert not {"type", "category", "memory_kind", "normalized_key"} & fields.keys()


def test_sensitive_or_unscoped_fact_is_rejected() -> None:
    result = MemoryValidator().validate(
        _proposal(content="API key 是 sk-1234567890"),
        scope=MemoryScope(tenant_id="cli"),
    )
    assert not result.accepted
    assert set(result.validation_codes) == {"sensitive_content", "missing_user_scope"}


@pytest.mark.parametrize("kind,subject,accepted", [
    ("profile", "user", True),
    ("profile", "我", True),
    ("profile", "老王", False),
    ("stable_preference", "assistant", True),
    ("stable_preference", "同事", False),
    ("ongoing_fact", "AuroraNotes", True),
])
def test_profile_and_preference_cards_describe_only_the_user(kind, subject, accepted):
    result = MemoryValidator().validate(
        _proposal(kind=kind, subject=subject, attribute="profession"),
        scope=MemoryScope(tenant_id="cli", user_id="u1"),
    )
    assert result.accepted is accepted
    if not accepted:
        assert "third_party_subject" in result.validation_codes


@pytest.mark.parametrize("text,kind,explicit", [
    ("更正一下我的职业：我现在是 Go 后端工程师。", "profile", True),
    ("我改主意了：以后回答请尽量详细。", "stable_preference", True),
    ("我是 Python 后端工程师。", "profile", True),
    ("再说一次，我的职业是 Python 后端工程师。", "profile", False),
    ("我同事老王是 Java 工程师。", "profile", False),
])
def test_explicit_user_statement_recognises_corrections(text, kind, explicit):
    from agent.core.memory.memory_claim_evidence import explicit_user_statement
    assert explicit_user_statement(text, kind) is explicit


@pytest.mark.parametrize("claim,quote,missing", [
    ("采样间隔从 1 秒改为 2 秒", "把采样间隔从一秒改为两秒后", ()),
    ("重试 12 次", "重试十二次", ()),
    ("压测 300 并发", "压测三百并发", ()),
    ("压测 500 并发", "压测三百并发", ("500",)),
])
def test_numeric_anchor_accepts_chinese_numerals_in_source(claim, quote, missing):
    from agent.core.memory.memory_claim_evidence import unsupported_anchors
    assert unsupported_anchors(claim, quote) == missing


@pytest.mark.parametrize("outcome,status,accepted", [
    ("中文导出仍然失败，尚未找到成功方案", "failure", True),
    ("Export failed; no successful output was produced.", "failure", True),
    ("导出没有成功", "success", False),
    ("尚未找到成功方案", "success", False),
    ("导出成功", "failure", False),
    ("Export succeeded.", "failure", False),
    ("导出成功", "success", True),
    ("在不同环境测试通过", "success", True),
])
def test_experience_polarity_respects_negation(outcome, status, accepted):
    proposal = _proposal(
        kind="reusable_experience", subject=None, attribute=None,
        content="本次 CSV 导出观察", summary="CSV 导出观察",
        keywords=("CSV", "Linux", "GBK"), applicability="Linux 导出 CSV",
        action="改用 GBK", outcome=outcome, outcome_status=status,
        limitations="只观察一次",
    )
    result = MemoryValidator().validate(proposal, scope=MemoryScope(user_id="u1"))
    assert result.accepted is accepted
