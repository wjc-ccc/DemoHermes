"""SessionMemoryReviewer：Session 增量到可持久化记忆候选的纯编排层。"""
from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from ..types import Session
from .memory_trace import (
    MemoryAiTraceFields,
    MemoryItemTrace,
    MemoryRuntimeTraceFields,
    MemoryTrace,
    MemoryTraceDecision,
    MemoryTraceOperation,
)
from .memory_validator import MemoryValidator
from .session_narrative import NarrativeItem
from .types.extraction_types import (
    MemoryExtractor,
    MemoryEvidence,
    MemoryProposal,
    MemoryValidationResult,
)
from .types.memory_source_types import TurnRecord
from .memory_item import UserMemoryItem


class MemoryReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_hash: str
    proposals: tuple[MemoryProposal, ...]
    validations: tuple[MemoryValidationResult, ...]
    user_items: tuple[UserMemoryItem, ...]
    narrative_items: tuple[NarrativeItem, ...]
    evidence: tuple[MemoryEvidence, ...]
    trace: MemoryTrace


class SessionMemoryReviewer:
    """提取、来源过滤、批内去重和 Trace 组装；不直接持有数据库连接。"""

    def __init__(
        self,
        extractor: MemoryExtractor,
        *,
        validator: MemoryValidator | None = None,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._extractor = extractor
        self._validator = validator or MemoryValidator()
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._clock = clock

    def review(self, turn: TurnRecord, session: Session) -> MemoryReviewResult:
        started = self._clock()
        turn_message_ids = {turn.user_message_id, *turn.assistant_message_ids}
        turn_tool_ids = set(turn.tool_call_ids)
        selected_messages = tuple(
            message
            for message in session.messages
            if message.message_id in turn_message_ids
        )
        selected_tools = tuple(
            tool
            for tool in session.tool_calls
            if tool.tool_call_id in turn_tool_ids
        )
        input_hash = hashlib.sha256(
            (
                turn.model_dump_json()
                + "|"
                + "|".join(message.model_dump_json() for message in selected_messages)
                + "|"
                + "|".join(tool.model_dump_json() for tool in selected_tools)
            ).encode("utf-8")
        ).hexdigest()
        proposals = tuple(self._extractor.extract(turn, session))
        allowed_messages = {
            message.message_id
            for message in session.messages
            if message.message_id == turn.user_message_id
            and message.role == "user"
            and message.metadata.get("source_type")
            not in {"memory_recalled", "system_injected"}
        }
        messages_by_id = {
            message.message_id: message
            for message in session.messages
            if message.message_id in allowed_messages
        }
        tools_by_id = {
            tool.tool_call_id: tool
            for tool in selected_tools
        }
        allowed_tools = set(tools_by_id)
        seen: dict[tuple[str, str], str] = {}
        validations: list[MemoryValidationResult] = []
        evidence: list[MemoryEvidence] = []
        traces: list[MemoryItemTrace] = []

        for proposal in proposals:
            codes: list[str] = []
            if not set(proposal.source_message_ids) <= allowed_messages:
                codes.append("untrusted_message_evidence")
            if not set(proposal.source_tool_call_ids) <= allowed_tools:
                codes.append("untrusted_tool_evidence")
            dedup_key = (proposal.target_layer.value, proposal.normalized_key)
            existing_hash = seen.get(dedup_key)
            proposal_hash = hashlib.sha256(proposal.content.encode("utf-8")).hexdigest()
            if existing_hash is not None:
                codes.append(
                    "duplicate_in_batch"
                    if existing_hash == proposal_hash
                    else "conflict_in_batch"
                )
            else:
                seen[dedup_key] = proposal_hash

            if codes:
                validation = MemoryValidationResult(
                    proposal_id=proposal.proposal_id,
                    accepted=False,
                    validation_codes=tuple(codes),
                )
            else:
                validation = self._validator.validate(proposal, scope=turn.scope)
            validations.append(validation)
            item = validation.user_item or validation.narrative_item
            if validation.accepted:
                for source_id in proposal.source_message_ids:
                    message = messages_by_id[source_id]
                    evidence.append(
                        MemoryEvidence(
                            proposal_id=proposal.proposal_id,
                            source_type="message",
                            source_id=source_id,
                            source_hash=hashlib.sha256(
                                message.model_dump_json().encode("utf-8")
                            ).hexdigest(),
                            observed_at=datetime.fromtimestamp(
                                message.created_at, timezone.utc
                            ),
                        )
                    )
                for source_id in proposal.source_tool_call_ids:
                    tool = tools_by_id[source_id]
                    evidence.append(
                        MemoryEvidence(
                            proposal_id=proposal.proposal_id,
                            source_type="tool_call",
                            source_id=source_id,
                            source_hash=hashlib.sha256(
                                tool.model_dump_json().encode("utf-8")
                            ).hexdigest(),
                            observed_at=datetime.fromtimestamp(
                                tool.created_at, timezone.utc
                            ),
                        )
                    )
            traces.append(
                MemoryItemTrace(
                    trace_item_id=self._id_factory(),
                    item_id=item.item_id if item is not None else None,
                    proposal_id=proposal.proposal_id,
                    source_type="memory_extraction",
                    source_ref=(
                        proposal.source_message_ids[0]
                        if proposal.source_message_ids
                        else (
                            proposal.source_tool_call_ids[0]
                            if proposal.source_tool_call_ids
                            else None
                        )
                    ),
                    layer=proposal.target_layer,
                    type=proposal.type,
                    decision=(
                        MemoryTraceDecision.ACCEPTED
                        if validation.accepted
                        else MemoryTraceDecision.REJECTED
                    ),
                    ai=MemoryAiTraceFields(
                        proposed_layer=proposal.target_layer,
                        proposed_type=proposal.type,
                        proposed_memory_kind=proposal.memory_kind,
                        proposed_keywords=proposal.keywords,
                        self_reported_confidence=proposal.confidence,
                        evidence_refs=(
                            *proposal.source_message_ids,
                            *proposal.source_tool_call_ids,
                        ),
                        proposed_mutation=proposal.proposed_mutation.value,
                        reason_code=proposal.reason_code,
                    ),
                    runtime=MemoryRuntimeTraceFields(
                        input_hash=input_hash,
                        content_hash=proposal_hash,
                        token_count=item.token_count if isinstance(item, UserMemoryItem) else None,
                        validator_version=self._validator.version,
                        validation_codes=validation.validation_codes,
                    ),
                )
            )

        trace = MemoryTrace(
            trace_id=self._id_factory(),
            operation=MemoryTraceOperation.VALIDATE,
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            task_id=turn.scope.task_id,
            validator_version=self._validator.version,
            latency_ms=max(0.0, (self._clock() - started) * 1000.0),
            items=tuple(traces),
        )
        return MemoryReviewResult(
            input_hash=input_hash,
            proposals=proposals,
            validations=tuple(validations),
            user_items=tuple(
                result.user_item
                for result in validations
                if result.accepted and result.user_item is not None
            ),
            narrative_items=tuple(
                result.narrative_item
                for result in validations
                if result.accepted and result.narrative_item is not None
            ),
            evidence=tuple(evidence),
            trace=trace,
        )
