"""Evolution 的 Skill 提案、信号与回放门公共类型。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

class SkillProposal(BaseModel):
    """程序性沉淀提案；由 Distiller / Reflector 产出，经 SkillRepository 落为候选。

    status 只是提案方的主张（LLM 可能自称 active），落盘时一律按 draft 处理；
    保留该字段是为了能审计「模型想直接上线」这件事。
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    body: str
    status: Literal["draft", "active"] = "draft"
    turn_id: str | None = None
    rationale: str = ""
    scope_key: str = ""
    required_tools: tuple[str, ...] = ()


class SkillPatchProposal(BaseModel):
    """对已有技能正文的候选修正；同样要过回放质量门才会落到生产。"""

    model_config = ConfigDict(frozen=True)

    skill_name: str
    old_string: str
    new_string: str
    turn_id: str | None = None
    rationale: str = ""


class ReplayGateResult(BaseModel):
    """baseline/candidate 逐 case 比较后的质量门结果。"""

    model_config = ConfigDict(frozen=True)

    admitted: bool
    reason: str
    regressed_case_ids: tuple[str, ...] = ()
    improved_case_ids: tuple[str, ...] = ()
    unchanged_case_ids: tuple[str, ...] = ()
    # 行为回退：case 仍然通过，但新增了工具失败
    behavior_regressed_case_ids: tuple[str, ...] = ()
    # 成本变化：候选相对 baseline 的上下文字符增量
    context_chars_delta: int = 0


class EvolutionSignal(BaseModel):
    """轻量 turn 信号（Phase 0.4 / Reflector 输入）；可缺省。"""

    model_config = ConfigDict(frozen=True)

    turn_id: str
    completed: bool = True
    interrupted: bool = False
    error: str | None = None
    iteration_count: int = 0
    tool_failures: tuple[str, ...] = Field(default_factory=tuple)
    # Phase 3：按执行顺序的工具名（含 use_skill）；供 SkillCreator / Optimizer
    tool_sequence: tuple[str, ...] = Field(default_factory=tuple)
    skills_used: tuple[str, ...] = Field(default_factory=tuple)
    # 本 turn 内曾失败后又成功（Hermes：errors overcome）
    recovered_from_failure: bool = False
