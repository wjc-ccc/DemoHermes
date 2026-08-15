"""Eval 公共类型（8.15 E1）。

载体：EvalSuite / EvalCase / EvalReport。
一次操作：EvalRunner.run(suite) → EvalReport。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EvalAssertionKind = Literal[
    "snapshot_contains",
    "reply_contains",
    "completed",
]

EvalSeedTarget = Literal["profile", "memory", "daily"]


class EvalSeedMemory(BaseModel):
    """跑 case 前写入 builtin Hot/Daily 的种子。"""

    model_config = ConfigDict(frozen=True)

    target: EvalSeedTarget
    content: str


class EvalAssertion(BaseModel):
    """对公开面的一条断言（snapshot 文本 / 回复 / 是否完成）。"""

    model_config = ConfigDict(frozen=True)

    kind: EvalAssertionKind
    value: str | bool


class EvalCase(BaseModel):
    """单条评测用例：可选种子记忆 + 用户消息 + 断言。"""

    model_config = ConfigDict(frozen=True)

    name: str
    user_text: str
    seeds: tuple[EvalSeedMemory, ...] = ()
    assertions: tuple[EvalAssertion, ...] = ()


class EvalSuite(BaseModel):
    """一组固定 EvalCase。"""

    model_config = ConfigDict(frozen=True)

    name: str
    cases: tuple[EvalCase, ...]


class EvalCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    failures: tuple[str, ...] = ()
    reply_text: str = ""
    completed: bool = False
    snapshot_prompt: str = ""


class EvalReport(BaseModel):
    """run(suite) 的汇总结果。"""

    model_config = ConfigDict(frozen=True)

    suite_name: str
    passed: bool
    passed_count: int
    total_count: int
    results: tuple[EvalCaseResult, ...] = Field(default_factory=tuple)
