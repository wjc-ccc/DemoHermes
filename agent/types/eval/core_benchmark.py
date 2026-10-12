"""核心长对话评测的配置、开关条件与复用计划。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkConfig:
    repeats: int = 3
    workers: int = 4
    perception_context_tokens: int = 20000
    full_context_tokens: int = 250000
    output_tokens: int = 1500
    max_iterations: int = 10
    model_retries: int = 1
    random_seed: int = 20261011
    bootstrap_samples: int = 2000
    quality_floor: float = .90
    noninferiority_margin: float = .02
    main_input_saving: float = .30
    review_fraction: float = .10


CONDITIONS = ((False, False), (False, True), (True, False), (True, True))
