"""运行装配与单轮执行的共享契约，不加载任何业务实现。"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from agent.types.action.config import ActionConfig
from agent.types.action.policy import AssemblyPolicy
from agent.types.context import ContextBudget
from agent.types.memory.scope import MemoryScope
from agent.core.config import LoopConfig
from agent.types.perception.admission import AdmissionConfig
from agent.types.perception.result import PerceptionConfig
from typing import Any, TYPE_CHECKING
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from agent.core.memory.config import (
        MemoryContextConfig, MemoryExtractionConfig, MemoryReviewConfig, MemoryScheduleConfig,
        MemoryExperienceConfig,
    )
    from agent.core.perception.config import JudgeConfig, TokenEstimateConfig


@dataclass(frozen=True)
class RuntimeConfig:
    """装配入口。每个可调类别只放一个 XxxConfig，字段名与类名对应。"""

    workers: int = 2
    use_memory: bool = True
    use_action: bool = True
    data_root: Path | None = None
    action_config: ActionConfig | None = None
    loop_config: LoopConfig | None = None
    perception_config: PerceptionConfig | None = None
    admission_config: AdmissionConfig | None = None
    judge_config: JudgeConfig | None = None
    token_estimate_config: TokenEstimateConfig | None = None
    memory_context_config: MemoryContextConfig | None = None
    memory_extraction_config: MemoryExtractionConfig | None = None
    memory_review_config: MemoryReviewConfig | None = None
    memory_schedule_config: MemoryScheduleConfig | None = None
    memory_experience_config: MemoryExperienceConfig | None = None
    manual_tokens: int | None = None
    experience_tokens: int | None = None
    context_budget: ContextBudget | None = None


@dataclass(frozen=True)
class RuntimeDescription:
    memory: bool = False
    action: bool = False
    storage: str = "sqlite"
    database: str | None = None
    model: str = "unknown"
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnContext:
    session_id: str
    turn_id: str
    source_message_id: str
    session_key: str = ""
    agent_id: str = "main"
    author_id: str = ""
    channel: str = ""
    memory_scope: MemoryScope | None = None
    assembly_policy: AssemblyPolicy | None = None


class EnvironmentSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    session_id: str
    turn_id: str
    captured_at: float
    cwd: str | None = None
    git: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1
