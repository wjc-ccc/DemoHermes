"""
Agent/eval 包

固定 suite 回放，产出 EvalReport（8.15）。
唯一公共入口：EvalRunner.run(suite) → EvalReport。
"""
from .ablation import run_ablation
from .classification import run_classification
from .runner import EvalRunner
from .replay import compare_reports
from .shadow import ShadowReplayRequest, ShadowReplayResult, run_shadow_replay
from .types import (
    AblationReport,
    AblationRequest,
    AblationResult,
    AblationVariant,
    ClassificationReport,
    ClassificationRequest,
    ClassificationSample,
    EvalCase,
    EvalCaseResult,
    EvalFailure,
    EvalReport,
    EvalSuite,
    EvalTurn,
    EvalTurnResult,
    LabelMetric,
    MemorySeed,
    ModelCallEvidence,
    ToolCallEvidence,
    TurnExpectation,
)

__all__ = [
    "AblationReport",
    "AblationRequest",
    "AblationResult",
    "AblationVariant",
    "ClassificationReport",
    "ClassificationRequest",
    "ClassificationSample",
    "EvalCase",
    "EvalCaseResult",
    "EvalFailure",
    "EvalReport",
    "EvalRunner",
    "EvalSuite",
    "EvalTurn",
    "EvalTurnResult",
    "LabelMetric",
    "MemorySeed",
    "ModelCallEvidence",
    "ToolCallEvidence",
    "TurnExpectation",
    "compare_reports",
    "run_ablation",
    "run_classification",
    "run_shadow_replay",
    "ShadowReplayRequest",
    "ShadowReplayResult",
]
