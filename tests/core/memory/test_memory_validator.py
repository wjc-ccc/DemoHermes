from __future__ import annotations

from agent.core.memory import MemoryLayer, MemoryScope, MemoryValidator, UserMemoryStatus
from agent.core.memory.types import MemoryProposal


def _proposal(**updates) -> MemoryProposal:
    values = {
        "proposal_id": "proposal-1",
        "target_layer": MemoryLayer.USER,
        "type": "preference",
        "normalized_key": "language.preference",
        "summary": "用户偏好中文",
        "content": "用户偏好中文回答",
        "keywords": ("中文",),
        "confidence": 0.9,
        "source_authority": "user_explicit",
        "source_message_ids": ("message-1",),
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
    assert result.user_item.status is UserMemoryStatus.ACTIVE
    assert result.user_item.keywords == ("中文",)


def test_inferred_user_fact_remains_proposed() -> None:
    result = MemoryValidator(id_factory=lambda: "memory-1").validate(
        _proposal(source_authority="model_inferred"),
        scope=MemoryScope(tenant_id="cli", user_id="u1"),
    )
    assert result.user_item is not None
    assert result.user_item.status is UserMemoryStatus.PROPOSED


def test_sensitive_or_unscoped_fact_is_rejected() -> None:
    result = MemoryValidator().validate(
        _proposal(content="API key 是 sk-1234567890"),
        scope=MemoryScope(tenant_id="cli"),
    )
    assert not result.accepted
    assert set(result.validation_codes) == {"sensitive_content", "missing_user_scope"}


def test_task_state_requires_task_scope() -> None:
    proposal = _proposal(
        target_layer=MemoryLayer.TASK,
        type="goal",
        normalized_key="goal.current",
        source_authority="user_explicit",
    )
    rejected = MemoryValidator().validate(
        proposal, scope=MemoryScope(tenant_id="cli", user_id="u1")
    )
    accepted = MemoryValidator(id_factory=lambda: "state-1").validate(
        proposal,
        scope=MemoryScope(tenant_id="cli", user_id="u1", task_id="task-1"),
    )
    assert rejected.validation_codes == ("missing_task_scope",)
    assert accepted.narrative_item is not None
