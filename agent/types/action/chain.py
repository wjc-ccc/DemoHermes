"""action / chain 数据契约。"""
from __future__ import annotations
import uuid
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from agent.types.action.execution import digest, now_ms


class ChainSegment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    segment_id: str = Field(min_length=1)
    prompt: str = ""
    output_schema: dict = Field(default_factory=lambda: {"type": "string"})
    semantic_gates: tuple[str, ...] = ()
    fact_gates: tuple[str, ...] = ()


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    artifact_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    scope_key: str
    plan_id: str
    step_id: str
    segment_id: str
    revision: int = 1
    body: Any
    content_hash: str
    input_hash: str
    source_refs: tuple[str, ...]
    status: Literal["pending", "accepted", "rejected"] = "pending"
    gate_fingerprint: str = ""
    created_at: int = Field(default_factory=now_ms)

    @classmethod
    def create(cls, *, body, **fields):
        return cls(body=body, content_hash=digest(body), **fields)
