"""运行装配与单轮执行的共享契约，不加载任何业务实现。"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from agent.types.action.config import ActionConfig
from agent.types.action.policy import AssemblyPolicy
from agent.types.context import ContextBudget
from agent.types.memory.scope import MemoryScope
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class RuntimeConfig:
    workers: int = 2
    use_memory: bool = True
    use_action: bool = False
    data_root: Path | None = None
    action_config: ActionConfig | None = None
    manual_tokens: int | None = None
    experience_tokens: int | None = None
    context_budget: ContextBudget | None = None


@dataclass(frozen=True)
class LoopConfig:
    max_iterations: int = 8
    model_retries: int = 2
    agent_id: str = "main"


@dataclass(frozen=True)
class RuntimeDescription:
    memory: bool = False
    action: bool = False
    storage: str = "memory"
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
