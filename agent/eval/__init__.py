"""
Agent/eval 包

固定 suite 回放，产出 EvalReport（8.15）。
唯一公共入口：EvalRunner.run(suite) → EvalReport。
"""
from .ablation import run_ablation
from .classification import run_classification
from .runner import EvalRunner
from .replay import compare_reports

__all__ = ['EvalRunner', 'compare_reports', 'run_ablation', 'run_classification']
