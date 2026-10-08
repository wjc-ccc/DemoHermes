"""跨 Session 批次的卡片提案审查与 Trace 组装。"""
# 职责：把提案与本批次真实消息、工具结果核对，生成验证结果和 Trace。
# 边界：不持有数据库连接，不处理已入库卡片之间的版本与冲突。
# 关联：上游是 memory_extractor.py，下游是 memory_validator.py 和 SQLite Committer。
from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from ..types import Session
from .memory_extractor import MemoryExtractor
from .memory_claim_evidence import (
    experience_outcome_signals, explicit_user_statement, has_source_overlap, unsupported_anchors,
)
from .memory_layer import MemoryLayer
from .memory_trace import (
    MemoryAiTraceFields,
    MemoryItemTrace,
    MemoryRuntimeTraceFields,
    MemoryTrace,
    MemoryTraceDecision,
    MemoryTraceOperation,
)
from .memory_validator import MemoryValidator
from .types.extraction_types import (
    MemoryExtractionIssue,
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
    extraction_issues: tuple[MemoryExtractionIssue, ...] = ()
    validations: tuple[MemoryValidationResult, ...]
    user_items: tuple[UserMemoryItem, ...]
    evidence: tuple[MemoryEvidence, ...]
    trace: MemoryTrace

### 对给定批次的 proposal 做证据审查；提交由下游 Committer 负责
class MemoryBatchReviewer:
    """提取、来源过滤、证据核验和 Trace 组装；不直接持有数据库连接。"""

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

    def review_batch(self, turns: tuple[TurnRecord, ...], sessions: tuple[Session, ...]) -> MemoryReviewResult:
        """
        review_batch(turns, sessions)
            要求 turns 与 sessions 一一对应

            本批消息 = 各 Session 里，id 属于这些 turn 的用户消息和助手消息
            本批工具 = 各 Session 里，id 属于这些 turn 的工具调用
            input_hash = 上述原文的指纹          # 只给审查和 Trace 用

            提案, 提取问题 = extract_batch(turns, sessions)

            可信用户消息 = 本批用户消息，且不是召回记忆、系统注入
            可信工具     = 本批工具，且名字不以 memory_ 开头
            每张提案按用户原文决定是否升为 user_explicit

            对每张提案:
                来源不在可信范围内                         → 拒绝
                引文为空，或不是对应原文的逐字片段           → 拒绝
                主张和引文对不上                           → 拒绝
                摘要、正文里的名称或数字不在引文里           → 拒绝
                经验卡的成败和引文相反                     → 拒绝
                否则交给 Validator 查结构、敏感内容和长度
                通过的提案，把每条来源记成证据
                通过或拒绝都记一条 Trace

            提取阶段格式坏掉的单张，也记成拒绝 Trace
            返回 提案、问题、审查结果、通过的卡片、证据、Trace
        """
        
        if not turns or len(turns) != len(sessions):
            raise ValueError("review batch requires aligned turns and sessions")
        started = self._clock()
        turn = turns[-1]
        turn_message_ids = {message_id for source in turns
                            for message_id in (source.user_message_id, *source.assistant_message_ids)}
        turn_tool_ids = {tool_id for source in turns for tool_id in source.tool_call_ids}

        ## 提取需要处理的字段信息  包含messages 和 tool
        selected_messages = tuple({
            message.message_id: message
            for session in sessions for message in session.messages
            if message.message_id in turn_message_ids
        }.values())
        selected_tools = tuple({
            tool.tool_call_id: tool
            for session in sessions for tool in session.tool_calls
            if tool.tool_call_id in turn_tool_ids
        }.values())
        input_hash = hashlib.sha256(
            (
                "|".join(source.model_dump_json() for source in turns)
                + "|"
                + "|".join(message.model_dump_json() for message in selected_messages)
                + "|"
                + "|".join(tool.model_dump_json() for tool in selected_tools)
            ).encode("utf-8")
        ).hexdigest()

        ### 提取对应的proposal提案信息
        extraction = self._extractor.extract_batch(turns, sessions)
        proposals = extraction.proposals
        user_ids = {source.user_message_id for source in turns}
        allowed_messages = {
            message.message_id
            for message in selected_messages
            if message.message_id in user_ids
            and message.role == "user"
            and message.metadata.get("source_type")
            not in {"memory_recalled", "system_injected"}
        }
        messages_by_id = {
            message.message_id: message
            for message in selected_messages
            if message.message_id in allowed_messages
        }
        proposals = tuple(_runtime_authority(proposal, messages_by_id)
                          for proposal in proposals)
        tools_by_id = {
            tool.tool_call_id: tool
            for tool in selected_tools
            if not tool.tool_name.startswith("memory_")
        }
        allowed_tools = set(tools_by_id)
        validations: list[MemoryValidationResult] = []
        evidence: list[MemoryEvidence] = []
        traces: list[MemoryItemTrace] = []

        for proposal in proposals:
            ### codes先简单格式校验 比如是否可信来源 是否引文存在 是否数字存在等等
            codes: list[str] = []
            if not set(proposal.source_message_ids) <= allowed_messages:
                codes.append("untrusted_message_evidence")
            if not set(proposal.source_tool_call_ids) <= allowed_tools:
                codes.append("untrusted_tool_evidence")
            for source_id in (*proposal.source_message_ids, *proposal.source_tool_call_ids):
                quote = proposal.evidence_quotes.get(source_id, "").strip()
                raw = (messages_by_id[source_id].content_text
                       if source_id in messages_by_id else
                       tools_by_id[source_id].result_text or ""
                       if source_id in tools_by_id else "")
                if not quote or quote not in raw:
                    codes.append("missing_exact_source_quote")
                    break
            claim = "\n".join(filter(None, (
                proposal.title, proposal.summary, proposal.content,
                proposal.applicability, proposal.action, proposal.outcome,
            )))
            quoted = "\n".join(proposal.evidence_quotes.values())
            if not has_source_overlap(claim, quoted):
                codes.append("claim_without_source_overlap")
            if unsupported_anchors("\n".join(filter(None, (
                proposal.summary, proposal.content,
            ))), quoted):
                codes.append("claim_anchor_missing_from_quote")
            if not _experience_polarity_supported(proposal):
                codes.append("experience_outcome_not_supported")
            proposal_hash = hashlib.sha256(proposal.content.encode("utf-8")).hexdigest()

            ### 如果codes不为空 则拒绝 否则交给validator校验
            if codes:
                validation = MemoryValidationResult(
                    proposal_id=proposal.proposal_id,
                    accepted=False,
                    validation_codes=tuple(codes),
                )
            else:
                ### 交给validator校验
                validation = self._validator.validate(proposal, scope=turn.scope)

            validations.append(validation)
            ### 如果通过 则把每条来源记成证据
            item = validation.user_item
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
                            excerpt=proposal.evidence_quotes.get(source_id),
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
                            excerpt=proposal.evidence_quotes.get(source_id),
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
                    layer=MemoryLayer.USER,
                    type=proposal.kind.value,
                    decision=(
                        MemoryTraceDecision.ACCEPTED
                        if validation.accepted
                        else MemoryTraceDecision.REJECTED
                    ),
                    ai=MemoryAiTraceFields(
                        proposed_layer=MemoryLayer.USER,
                        proposed_kind=proposal.kind,
                        proposed_keywords=proposal.keywords,
                        self_reported_confidence=proposal.confidence,
                        evidence_refs=(
                            *proposal.source_message_ids,
                            *proposal.source_tool_call_ids,
                        ),
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

        for issue in extraction.issues:
            traces.append(MemoryItemTrace(
                trace_item_id=self._id_factory(), proposal_id=issue.proposal_id,
                source_type="memory_extraction", layer=MemoryLayer.USER,
                decision=MemoryTraceDecision.REJECTED,
                runtime=MemoryRuntimeTraceFields(
                    input_hash=input_hash, validator_version=self._validator.version,
                    validation_codes=(issue.code,),
                ),
            ))
        ### 记录本次记忆提取的trace逻辑
        trace = MemoryTrace(
            trace_id=self._id_factory(),
            operation=MemoryTraceOperation.VALIDATE,
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            validator_version=self._validator.version,
            latency_ms=max(0.0, (self._clock() - started) * 1000.0),
            items=tuple(traces),
        )

        ### 返回最终记忆提取的结果 保存trace proposal validation user_items evidence
        return MemoryReviewResult(
            input_hash=input_hash,
            proposals=proposals,
            extraction_issues=extraction.issues,
            validations=tuple(validations),
            user_items=tuple(
                result.user_item
                for result in validations
                if result.accepted and result.user_item is not None
            ),
            evidence=tuple(evidence),
            trace=trace,
        )


def _runtime_authority(proposal, messages_by_id):
    """根据用户原文识别明确陈述，不信任模型自行标注的来源等级。"""
    if (proposal.source_authority != "model_inferred"
        or len(proposal.source_message_ids) != 1 or proposal.source_tool_call_ids):
        return proposal
    source_id = proposal.source_message_ids[0]
    message = messages_by_id.get(source_id)
    quote = proposal.evidence_quotes.get(source_id, "").strip()
    if message is None or not quote or quote not in message.content_text:
        return proposal
    if (proposal.kind.value != "reusable_experience"
        and explicit_user_statement(message.content_text, proposal.kind.value)):
        return proposal.model_copy(update={"source_authority": "user_explicit"})
    return proposal


def _experience_polarity_supported(proposal) -> bool:
    if proposal.kind is None or proposal.kind.value != "reusable_experience":
        return True
    quoted = "\n".join(proposal.evidence_quotes.values())
    positive, negative = experience_outcome_signals(quoted)
    status = getattr(proposal.outcome_status, "value", proposal.outcome_status)
    if status == "success" and negative and not positive:
        return False
    if status == "failure" and positive and not negative:
        return False
    return True
