"""经验事后评判的跨模块协议与持久作业版本。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Literal, Mapping, Protocol, Sequence


Adoption = Literal["yes", "no", "unknown"]
Effect = Literal["helpful", "harmful", "neutral", "unknown"]
Relation = Literal["duplicate", "complementary", "conflict", "unrelated"]
FEEDBACK_VERSION = "memory-feedback-v1"


def feedback_handler_version(model_key: str) -> str:
    """按受益模型隔离领取，避免不同模型的消费者互相抢占作业。"""
    return FEEDBACK_VERSION + ":" + hashlib.sha256(model_key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class MemoryUsageRequestItem:
    decision_id: str
    card_id: str
    card_revision: int
    turn_id: str
    model_key: str
    card: Mapping[str, Any]
    goal: str
    evidence: tuple[dict, ...] = ()
    before_evidence: tuple[dict, ...] = ()
    evidence_complete: bool = True


@dataclass(frozen=True)
class MemoryUsageResultItem:
    decision_id: str
    adopted: Adoption = "unknown"
    effect: Effect = "unknown"
    reason: str = "insufficient_observable_evidence"
    evidence_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"adopted": self.adopted, "effect": self.effect,
                "reason": self.reason, "evidence_refs": list(self.evidence_refs)}


@dataclass(frozen=True)
class MemoryRelationItem:
    item_id: str
    relation: Relation = "unrelated"
    confidence: float | None = None


class ExperienceJudge(Protocol):
    id: str

    @property
    def is_usable(self) -> bool: ...

    def judge_usage(self, requests: Sequence[MemoryUsageRequestItem]) -> list[MemoryUsageResultItem]: ...

    def judge_relations(self, anchor: Any, candidates: Sequence[Any]) -> tuple[MemoryRelationItem, ...]: ...

    def judge_merge(self, cards: Sequence[Any], draft: Mapping[str, Any]) -> bool: ...
