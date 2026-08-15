"""
Agent/eval 包

固定 suite 回放，产出 EvalReport（8.15）。
唯一公共入口：EvalRunner.run(suite) → EvalReport。
"""
from .runner import EvalRunner
from .types import (
    EvalAssertion,
    EvalCase,
    EvalCaseResult,
    EvalReport,
    EvalSeedMemory,
    EvalSuite,
)

__all__ = [
    "EvalAssertion",
    "EvalCase",
    "EvalCaseResult",
    "EvalReport",
    "EvalRunner",
    "EvalSeedMemory",
    "EvalSuite",
]
