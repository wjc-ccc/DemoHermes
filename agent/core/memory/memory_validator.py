"""MemoryProposal 的确定性层级、证据与敏感信息校验。"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable

from .memory_item import content_sha256
from .memory_layer import MemoryLayer, validate_item_type
from .memory_scope import MemoryScope
from .session_narrative import NarrativeItem
from .types.extraction_types import MemoryProposal, MemoryValidationResult, ProposedMutation
from .memory_item import UserMemoryItem, UserMemoryStatus


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|secret|token)\b"),
    re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"(?:密码|密钥|身份证号|访问令牌)"),
)

_DIRECT_AUTHORITIES = frozenset({"user_explicit", "tool_verified", "manual"})


class MemoryValidator:
    version = "memory-validator-v1"

    def __init__(
        self,
        *,
        id_factory: Callable[[], str] | None = None,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._token_counter = token_counter or (
            lambda text: len(text.split()) if text.isascii() else len(text)
        )

    def validate(
        self,
        proposal: MemoryProposal,
        *,
        scope: MemoryScope,
    ) -> MemoryValidationResult:
        codes: list[str] = []
        try:
            validate_item_type(proposal.target_layer, proposal.type)
        except ValueError:
            codes.append("invalid_item_type")
        if not proposal.source_message_ids and not proposal.source_tool_call_ids:
            codes.append("missing_evidence")
        if any(pattern.search(proposal.content) for pattern in _SENSITIVE_PATTERNS):
            codes.append("sensitive_content")
        if proposal.proposed_mutation is ProposedMutation.NOOP:
            codes.append("proposal_noop")
        if proposal.target_layer is MemoryLayer.USER and scope.user_id is None:
            codes.append("missing_user_scope")
        if proposal.target_layer is MemoryLayer.TASK and scope.task_id is None:
            codes.append("missing_task_scope")
        if codes:
            return MemoryValidationResult(
                proposal_id=proposal.proposal_id,
                accepted=False,
                validation_codes=tuple(codes),
            )

        item_id = self._id_factory()
        content_hash = content_sha256(proposal.content)
        metadata = {
            **dict(proposal.metadata),
            "proposal_id": proposal.proposal_id,
            "reason_code": proposal.reason_code,
        }
        if proposal.target_layer is MemoryLayer.USER:
            status = (
                UserMemoryStatus.ACTIVE
                if proposal.source_authority in _DIRECT_AUTHORITIES
                else UserMemoryStatus.PROPOSED
            )
            item = UserMemoryItem(
                item_id=item_id,
                type=proposal.type,
                source_type="memory_extraction",
                source_ref=proposal.source_message_ids[0]
                if proposal.source_message_ids
                else proposal.source_tool_call_ids[0],
                scope=scope,
                title=proposal.title,
                content=proposal.content,
                summary=proposal.summary,
                priority=50,
                token_count=max(0, int(self._token_counter(proposal.content))),
                content_hash=content_hash,
                metadata=metadata,
                memory_kind=proposal.memory_kind,
                normalized_key=proposal.normalized_key,
                keywords=proposal.keywords,
                status=status,
                confidence=proposal.confidence,
                source_authority=proposal.source_authority,
                valid_from=proposal.valid_from,
                valid_until=proposal.valid_until,
                supersedes_item_id=proposal.supersedes_item_id,
            )
            return MemoryValidationResult(
                proposal_id=proposal.proposal_id,
                accepted=True,
                validation_codes=("accepted",),
                user_item=item,
            )

        narrative_item = NarrativeItem(
            item_id=item_id,
            session_id=scope.session_id or scope.task_id or "",
            event_type="observation",
            what_happened=proposal.content,
            reason=proposal.reason_code,
            author="memory_extractor",
            status="proposed",
            metadata={**metadata, "model_claim": proposal.model_dump(mode="json")},
        )
        return MemoryValidationResult(
            proposal_id=proposal.proposal_id,
            accepted=True,
            validation_codes=("accepted",),
            narrative_item=narrative_item,
        )
