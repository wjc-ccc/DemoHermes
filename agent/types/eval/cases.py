"""Eval 的版本化考卷、类型化期望与真实 turn 证据。"""
from __future__ import annotations
import hashlib
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


MemorySeedTarget = Literal["policy", "project", "user"]


class MemorySeed(BaseModel):
    """Case 运行前写入隔离 Memory 的已知事实。"""

    model_config = ConfigDict(frozen=True)

    target: MemorySeedTarget
    content: str


class TurnExpectation(BaseModel):
    """对一次真实 turn 的类型化行为期望。"""

    model_config = ConfigDict(frozen=True)

    # skill_contains 断言的是 L1 摘要里出现了技能名；
    # skill_loaded 断言技能正文真的被 use_skill 加载过——只有后者能证明技能起了作用
    skill_loaded: tuple[str, ...] = ()
    skill_not_loaded: tuple[str, ...] = ()
    max_tool_failures: int | None = None
    context_contains: tuple[str, ...] = ()
    context_lacks: tuple[str, ...] = ()
    memory_contains: tuple[str, ...] = ()
    memory_lacks: tuple[str, ...] = ()
    experience_contains: tuple[str, ...] = ()
    experience_lacks: tuple[str, ...] = ()
    skill_contains: tuple[str, ...] = ()
    skill_lacks: tuple[str, ...] = ()
    reply_contains: tuple[str, ...] = ()
    reply_lacks: tuple[str, ...] = ()
    completed: bool | None = True
    persistence_succeeded: bool | None = True
    max_context_chars: int | None = None


class EvalTurn(BaseModel):
    """Case 中按顺序执行的一次用户 turn。"""

    model_config = ConfigDict(frozen=True)

    user_text: str
    expectation: TurnExpectation = Field(default_factory=TurnExpectation)


class EvalCase(BaseModel):
    """彼此隔离、内部可多 turn 的评测场景。"""

    model_config = ConfigDict(frozen=True)

    case_id: str
    turns: tuple[EvalTurn, ...]
    seeds: tuple[MemorySeed, ...] = ()
    tags: tuple[str, ...] = ()
    channel: str = "cli"
    user_id: str = ""

    @model_validator(mode="after")
    def _require_turn(self) -> "EvalCase":
        if not self.turns:
            raise ValueError("EvalCase.turns 不能为空")
        return self


class EvalSuite(BaseModel):
    """有版本的一组固定 EvalCase。

    target_skills 声明「这份考卷是哪些技能的考题」：候选技能据此找到自己的
    目标 case，同一份 suite 里的其余 case 天然充当回归集。
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    cases: tuple[EvalCase, ...]
    target_skills: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_cases(self) -> "EvalSuite":
        if not self.cases:
            raise ValueError("EvalSuite.cases 不能为空")
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("EvalSuite.case_id 必须唯一")
        return self

    def fingerprint(self) -> str:
        """考卷内容指纹；防止两份报告用同名同版本但不同期望做比较。"""
        payload = self.model_dump_json(exclude={"target_skills"})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
