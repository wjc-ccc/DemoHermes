"""
Agent/evolution 包

自我进化机制（8.04）：旁路于 AgentLoop，改变下一 turn 的上下文。

公开 seam：
  - EvolutionScheduler：commit 后扇出 / 每 N turn refine
  - RuleBasedEvolutionReflector / EvolutionReflector：策略复盘
  - EvolutionCommitGate：写入门控
  - PlaybookCurator：insight → apply；record_failure / refine
  - GrowAndRefineCompactor：超限结构化缩减（不摘要）
  - EvolutionSignalCollector：EventBus 轻量信号
  - SkillCreator / SkillOptimizer / SkillLifecycleGate：程序性 Skill 产物
"""
from .compactor import GrowAndRefineCompactor
from .gate import EvolutionCommitGate
from .memory_curator import PlaybookCurator
from .reviewer import RuleBasedEvolutionReflector
from .scheduler import EvolutionScheduler
from .signals import EvolutionSignalCollector
from .skill_creator import SkillCreator
from .skill_gate import SkillLifecycleGate
from .skill_optimizer import SkillOptimizer
from .types import EvolutionInsight, EvolutionSignal, GateDecision

__all__ = [
    "EvolutionCommitGate",
    "EvolutionInsight",
    "EvolutionScheduler",
    "EvolutionSignal",
    "EvolutionSignalCollector",
    "GateDecision",
    "GrowAndRefineCompactor",
    "PlaybookCurator",
    "RuleBasedEvolutionReflector",
    "SkillCreator",
    "SkillLifecycleGate",
    "SkillOptimizer",
]
