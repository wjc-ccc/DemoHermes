"""可留档的守卫/闸门判定；判定不等于外部执行结果。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict


class GateDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verdict: Literal["accept", "repair", "retry", "block", "escalate", "unknown"]
    layer: Literal["syntax", "structure", "semantics", "facts", "pre", "post"]
    reasons: tuple[str, ...] = ()
    detail: str = ""
    evidence_refs: tuple[str, ...] = ()
    effective_value: dict | None = None
    retryable: bool = False
    validator_version: str = "1"

