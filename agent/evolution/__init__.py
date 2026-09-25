"""
Agent/evolution 包

自我进化机制（8.04）：旁路于 AgentLoop，改变下一 turn 的上下文。
Hermes 对齐：observe→distill→reuse→refine（SkillSelector / Distiller / LLM Reflector）。
"""
from .assembly import AssemblyChoice, AssemblyPolicy, AssemblyRecorder
from .audit import EvolutionAuditEntry
from .env_snapshot import EnvironmentSnapshot, EnvironmentSnapshotter
from .llm_reflector import LlmEvolutionReflector
from .scheduler import EvolutionScheduler
from .signals import EvolutionSignalCollector
from .skill_creator import SkillCreator
from .skill_distiller import TrajectorySkillDistiller
from .skill_evolution import SkillEvolution, suite_for_skill
from .skill_gate import SkillLifecycleGate
from .skill_optimizer import SkillOptimizer
from .skill_selector import SkillSelector
from .turn_facts import build_turn_facts
from .types import (
    EvolutionSignal,
    ReplayGateResult,
    SkillPatchProposal,
    SkillProposal,
)

__all__ = [
    "AssemblyChoice",
    "AssemblyPolicy",
    "AssemblyRecorder",
    "EnvironmentSnapshot",
    "EnvironmentSnapshotter",
    "EvolutionAuditEntry",
    "EvolutionScheduler",
    "EvolutionSignal",
    "EvolutionSignalCollector",
    "LlmEvolutionReflector",
    "ReplayGateResult",
    "SkillCreator",
    "SkillEvolution",
    "SkillLifecycleGate",
    "SkillOptimizer",
    "SkillPatchProposal",
    "SkillProposal",
    "SkillSelector",
    "TrajectorySkillDistiller",
    "build_turn_facts",
    "suite_for_skill",
]
