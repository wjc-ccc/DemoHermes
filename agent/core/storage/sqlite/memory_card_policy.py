"""卡片写入共用的证据时间、权威与去重规则。

职责：为后台提交和显式更正提供同一套旧证据保护。
边界：不执行 SQL，也不决定是否从对话提炼新卡。
关联：MemoryReviewCommitter 和 SqliteMemoryCardMutationService 调用。
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.types.memory.categories import UserCardKind


def evidence_time(item):
    observed = tuple(ref.observed_at for ref in item.evidence if ref.observed_at)
    return max(observed) if observed else item.created_at


def authority_rank(authority: str) -> int:
    return {"model_inferred": 0, "tool_verified": 1,
            "user_explicit": 2, "manual": 2}.get(authority, 0)


def stronger_or_newer(previous, candidate) -> bool:
    if authority_rank(previous.source_authority) > authority_rank(candidate.source_authority):
        return True
    previous_at, proposed_at = evidence_time(previous), evidence_time(candidate)
    return bool(previous_at and (not proposed_at or proposed_at <= previous_at))


def can_restore_after_delete(candidate, tombstones) -> bool:
    if candidate.source_authority != "user_explicit":
        return False
    proposed_at = evidence_time(candidate)
    return bool(proposed_at and all(
        tombstone.updated_at and proposed_at > tombstone.updated_at
        for tombstone in tombstones
    ))


def is_explicit_correction(candidate, active) -> bool:
    if candidate.source_authority == "user_explicit":
        return True
    return (candidate.source_authority == "tool_verified"
            and all(card.source_authority == "model_inferred" for card in active))


def merge_card_evidence(existing, candidate):
    evidence = {ref.model_dump_json(): ref for ref in (*existing.evidence, *candidate.evidence)}
    keywords = tuple(dict.fromkeys((*existing.keywords, *candidate.keywords)))
    if existing.kind is UserCardKind.REUSABLE_EXPERIENCE:
        keywords = keywords[:8]
    return tuple(evidence.values()), keywords


def source_is_not_older(item, source_at: datetime) -> bool:
    previous_at = evidence_time(item) or datetime.min.replace(tzinfo=timezone.utc)
    return previous_at <= source_at

