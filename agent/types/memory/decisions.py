"""经验曝光、事后使用评价与近邻整理共用的持久化决策协议。"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UsageJudgmentItem(BaseModel):
    """判官只能描述可观察的采纳和效果，不把任务成败当作单卡因果证据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adopted: Literal["yes", "no", "unknown"]
    effect: Literal["helpful", "harmful", "neutral", "unknown"]
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _effect_requires_adoption(self) -> "UsageJudgmentItem":
        if self.adopted != "yes" and self.effect in {"helpful", "harmful", "neutral"}:
            raise ValueError("未确认采纳的卡片不能判定产生效果")
        return self


class DecisionItem(TypedDict):
    """数据库行的已解析投影；result_json 始终保留当时的 context。"""

    id: str
    scope_key: str
    card_id: str
    card_revision: int
    decision_type: Literal["usage", "merge"]
    turn_id: str | None
    model_key: str
    judge_key: str | None
    adopted: str | None
    effect: str | None
    reason: str | None
    evidence_refs: tuple[str, ...]
    result_json: dict[str, Any]
    dedupe_key: str
    created_at: int
    judged_at: int | None
    counted_at: int | None
