"""
Agent/evolution 包

自我进化机制（8.04）：旁路于 AgentLoop，改变下一 turn 的上下文。
Hermes 对齐：observe→distill→reuse→refine（SkillSelector / Distiller / LLM Reflector）。
"""
from .assembly import AssemblyChoice, AssemblyPolicy, AssemblyRecorder
from .audit import EvolutionAuditEntry, EvolutionAuditLog
from .compactor import GrowAndRefineCompactor
from .env_snapshot import EnvironmentSnapshot, EnvironmentSnapshotter
from .gate import EvolutionCommitGate
from .held_out import HeldOutSelector, HeldOutTask
from .llm_reflector import LlmEvolutionReflector
from .memory_curator import PlaybookCurator
from .reviewer import RuleBasedEvolutionReflector
from .scheduler import EvolutionScheduler
from .signals import EvolutionSignalCollector
from .skill_creator import SkillCreator
from .skill_distiller import TrajectorySkillDistiller
from .skill_gate import SkillLifecycleGate
from .skill_optimizer import SkillOptimizer
from .skill_selector import SkillSelector
from .types import EvolutionInsight, EvolutionSignal, GateDecision, SkillProposal

__all__ = [
    "AssemblyChoice",
    "AssemblyPolicy",
    "AssemblyRecorder",
    "EnvironmentSnapshot",
    "EnvironmentSnapshotter",
    "EvolutionAuditEntry",
    "EvolutionAuditLog",
    "EvolutionCommitGate",
    "EvolutionInsight",
    "EvolutionScheduler",
    "EvolutionSignal",
    "EvolutionSignalCollector",
    "GateDecision",
    "GrowAndRefineCompactor",
    "HeldOutSelector",
    "HeldOutTask",
    "LlmEvolutionReflector",
    "PlaybookCurator",
    "RuleBasedEvolutionReflector",
    "SkillCreator",
    "SkillLifecycleGate",
    "SkillOptimizer",
    "SkillProposal",
    "SkillSelector",
    "TrajectorySkillDistiller",
]
