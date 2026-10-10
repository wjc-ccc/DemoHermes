"""MemoryProposal 的确定性层级、证据与敏感信息校验。"""
# 职责：检查提案字段、scope、敏感内容及经验结构，并构造 User 卡。
# 边界：不读取证据原文；逐字引文与语义线索由 Reviewer 在调用前检查。
# 关联：后台提炼与 memory_edit 卡片更正共用这里的结构校验。
from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Callable

from agent.types.memory.items import content_sha256, UserMemoryItem
from .memory_claim_evidence import experience_outcome_signals
from agent.types.memory.categories import UserCardKind, ExperienceOutcome, UserCardStatus
from agent.types.memory.scope import MemoryScope
from agent.types.memory.extraction import MemoryProposal, MemoryValidationResult


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|secret|token)\b"),
    re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"(?:密码|密钥|身份证号|访问令牌)"),
)

# 用户卡片只描述用户本人；同事、家人等第三方的身份与习惯不入卡。
_CARD_SUBJECTS = {
    UserCardKind.PROFILE: ("user",),
    UserCardKind.STABLE_PREFERENCE: ("user", "assistant"),
}

_SOURCE_AUTHORITIES = frozenset({"user_explicit", "tool_verified", "manual", "model_inferred"})
_GENERIC_KEYWORDS = frozenset({
    "工作", "问题", "帮助", "事情", "任务", "经验", "项目", "用户", "助手",
    "work", "problem", "help", "task", "experience", "project", "user", "assistant",
})


def _normalized_keywords(keywords: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFKC", value).strip() for value in keywords)

## 对于user类型的proposal卡片进行校验
class MemoryValidator:
    version = "memory-validator-v3"

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

    # 到这里，原文对不对得上已经查过了。这一步只看这张卡本身像不像一张能保存的卡：
    # 有没有来源和引文，正文里有没有密码、密钥或身份证号，是不是这个用户的卡，篇幅是否太长。
    # 经验卡要讲清在什么情况下做了什么、结果如何、有什么限制，成败不能自己打架，关键词要具体。
    # 事实卡只写事实，不要混进经验字段；身份只记用户本人，偏好只记用户或助手。
    # 都说得通就做成一张待入库的卡，有一处不行就拒绝，不写库。
    def validate(
        self,
        proposal: MemoryProposal,
        *,
        scope: MemoryScope,
    ) -> MemoryValidationResult:
        codes: list[str] = []
        if not proposal.source_message_ids and not proposal.source_tool_call_ids:
            codes.append("missing_evidence")
        if any(
            not proposal.evidence_quotes.get(source_id, "").strip()
            for source_id in (*proposal.source_message_ids, *proposal.source_tool_call_ids)
        ):
            codes.append("missing_source_quote")
        card_text = "\n".join(filter(None, (
            proposal.title, proposal.summary, proposal.content,
            proposal.applicability, proposal.action, proposal.outcome, proposal.limitations,
            *proposal.keywords,
        )))
        if any(pattern.search(card_text) for pattern in _SENSITIVE_PATTERNS):
            codes.append("sensitive_content")
        if proposal.source_authority not in _SOURCE_AUTHORITIES:
            codes.append("invalid_source_authority")
        if scope.user_id is None:
            codes.append("missing_user_scope")
        if len(proposal.content) > (900 if proposal.kind is UserCardKind.REUSABLE_EXPERIENCE else 500):
            codes.append("card_claim_too_long")
        if proposal.kind is UserCardKind.REUSABLE_EXPERIENCE:
            if not all((proposal.applicability, proposal.action, proposal.outcome, proposal.limitations)):
                codes.append("incomplete_experience")
            if proposal.outcome_status is None:
                codes.append("missing_experience_outcome_status")
            positive, negative = experience_outcome_signals(proposal.outcome or "")
            if ((proposal.outcome_status is ExperienceOutcome.SUCCESS and negative)
                or (proposal.outcome_status is ExperienceOutcome.FAILURE and positive)):
                codes.append("experience_outcome_conflict")
            keywords = _normalized_keywords(proposal.keywords)
            unique = {value.casefold() for value in keywords if value}
            if (not 3 <= len(keywords) <= 8
                or len(unique) != len(keywords)
                or any(len(value) > 64 or value.casefold() in _GENERIC_KEYWORDS for value in keywords)):
                codes.append("invalid_experience_keywords")
        else:
            if any((proposal.applicability, proposal.action, proposal.outcome,
                    proposal.limitations, proposal.outcome_status)):
                codes.append("unexpected_experience_fields")
            subject = proposal.slot_key.split(":", 2)[1]
            if subject not in _CARD_SUBJECTS.get(proposal.kind, (subject,)):
                codes.append("third_party_subject")
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
        item = UserMemoryItem(
                item_id=item_id,
                kind=proposal.kind,
                slot_key=proposal.slot_key,
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
                keywords=_normalized_keywords(proposal.keywords),
                status=UserCardStatus.ACTIVE,
                confidence=proposal.confidence,
                source_authority=proposal.source_authority,
                valid_from=proposal.valid_from,
                valid_until=proposal.valid_until,
                applicability=proposal.applicability,
                action=proposal.action,
                outcome=proposal.outcome,
                limitations=proposal.limitations,
                outcome_status=proposal.outcome_status,
        )
        return MemoryValidationResult(
            proposal_id=proposal.proposal_id,
            accepted=True,
            validation_codes=("accepted",),
            user_item=item,
        )
