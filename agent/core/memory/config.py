"""记忆提炼与后台审查的架构超参数。默认值保持当前生产行为。"""
from __future__ import annotations

from dataclasses import dataclass
import math


def _positive_int(name: str, value: int, *, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数")


def _non_negative_number(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{name} 必须是大于等于 0 的数")


def experience_model_key(model) -> str:
    """经验收益按受指导的主模型隔离，判官身份另存在判定结果中。"""
    return f"{getattr(model, 'name', type(model).__name__)}:{getattr(model, 'model_name', '')}"


@dataclass(frozen=True)
class MemoryExtractionConfig:
    """控制提炼输入预算、来源切块和旧卡扫描。

    预算按估算器的 token 计，切块宽度按字符计。准入开启时，分组先扣掉
    ``admission_reserve_tokens``，真正发请求时仍用完整的 ``input_token_budget``。
    """
    
    input_token_budget: int = 16_000
    admission_review: bool = True
    admission_reserve_tokens: int = 1_024
    prompt_headroom_tokens: int = 1_024
    source_reserve_tokens: int = 2_048
    fragment_max_chars: int = 2_048
    fragment_overlap_cap: int = 128
    fragment_overlap_divisor: int = 8
    context_user_chars: int = 160
    related_card_limit: int = 8
    card_page_size: int = 100
    card_scan_limit: int = 400

    def __post_init__(self) -> None:
        if not isinstance(self.admission_review, bool):
            raise ValueError("admission_review 必须是布尔值")
        for name in (
            "input_token_budget",
            "fragment_max_chars",
            "fragment_overlap_divisor",
            "context_user_chars",
            "related_card_limit",
            "card_page_size",
            "card_scan_limit",
        ):
            _positive_int(name, getattr(self, name))
        for name in (
            "admission_reserve_tokens",
            "prompt_headroom_tokens",
            "source_reserve_tokens",
            "fragment_overlap_cap",
        ):
            _positive_int(name, getattr(self, name), minimum=0)


@dataclass(frozen=True)
class MemoryReviewConfig:
    """后台提炼作业的租约、重试和轮询。"""

    lease_seconds: float = 30.0
    max_attempts: int = 3
    backoff_seconds: float = 60.0
    poll_interval: float = 0.5
    shutdown_grace: float = 5.0

    def __post_init__(self) -> None:
        _positive_int("max_attempts", self.max_attempts)
        for name in ("lease_seconds", "poll_interval", "shutdown_grace"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} 必须是正数")
        _non_negative_number("backoff_seconds", self.backoff_seconds)


@dataclass(frozen=True)
class MemoryContextConfig:
    """每轮注入模型的手册和经验预算。卡片本身仍按需检索，不整库常驻。"""

    manual_tokens: int = 1200
    experience_tokens: int = 600
    experience_limit: int = 3

    def __post_init__(self) -> None:
        _positive_int("manual_tokens", self.manual_tokens)
        _positive_int("experience_tokens", self.experience_tokens)
        _positive_int("experience_limit", self.experience_limit)


@dataclass(frozen=True)
class MemoryExperienceConfig:
    """经验效用、异步评判与有界场景合并共用的策略。"""

    enabled: bool = True
    maturity_prior: float = 3.0
    maturity_weight: float = 0.5
    minimum_decisive_count: int = 5
    disable_maturity_threshold: float = -0.25
    unused_threshold: int = 5
    unused_weight: float = 0.75
    judge_input_tokens: int = 4096
    judge_batch_size: int = 3
    embedding_top_k: int = 5
    embedding_batch_size: int = 8
    embedding_input_tokens: int = 4096
    embedding_max_batches: int = 1
    embedding_min_similarity: float = 0.0
    maintenance_scan_limit: int = 100
    merge_min_cards: int = 3
    merge_max_cards: int = 5
    merge_input_tokens: int = 4096
    merge_output_tokens: int = 1024
    merge_max_per_pass: int = 1
    merge_minimum_decisive_count: int = 5
    merge_accept_maturity: float = 0.5
    merge_trial_limit: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        for name in ("minimum_decisive_count", "unused_threshold", "judge_input_tokens",
                     "judge_batch_size", "embedding_top_k", "embedding_batch_size",
                     "embedding_input_tokens", "embedding_max_batches", "maintenance_scan_limit",
                     "merge_min_cards", "merge_max_cards", "merge_input_tokens", "merge_output_tokens",
                     "merge_max_per_pass", "merge_minimum_decisive_count", "merge_trial_limit"):
            _positive_int(name, getattr(self, name))
        if self.judge_batch_size > 3 or self.merge_min_cards < 3 or self.merge_max_cards < self.merge_min_cards:
            raise ValueError("每批最多评判3张卡，合并至少需要3张且不能超过合并上限")
        for name in ("maturity_prior", "maturity_weight", "disable_maturity_threshold",
                     "unused_weight", "embedding_min_similarity", "merge_accept_maturity"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} 必须是有限数值")
        if self.maturity_prior <= 0 or not 0 <= self.maturity_weight <= 1:
            raise ValueError("成熟度先验必须为正数，权重必须在0到1之间")
        if not -1 <= self.disable_maturity_threshold <= 0 or not 0 <= self.merge_accept_maturity <= 1:
            raise ValueError("成熟度停用或合并门槛超出范围")
        if not 0 < self.unused_weight <= 1 or not -1 <= self.embedding_min_similarity <= 1:
            raise ValueError("未采纳权重或向量相似度超出范围")


@dataclass(frozen=True)
class MemoryScheduleConfig:
    """跨会话累计完成轮次后，何时安排一次提炼。

    默认在第 1、3、6、10 轮提炼，从第 15 轮起每 5 轮一次。
    重置会话和进程退出仍会立刻刷出尚未安排的轮次。
    """

    early_ordinals: tuple[int, ...] = (1, 3, 6, 10)
    steady_start: int = 15
    steady_interval: int = 5

    def __post_init__(self) -> None:
        if not self.early_ordinals or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in self.early_ordinals):
            raise ValueError("early_ordinals 必须是正整数")
        if len(set(self.early_ordinals)) != len(self.early_ordinals):
            raise ValueError("early_ordinals 不能重复")
        _positive_int("steady_start", self.steady_start)
        _positive_int("steady_interval", self.steady_interval)

    def is_boundary(self, ordinal: int) -> bool:
        if ordinal in self.early_ordinals:
            return True
        if ordinal < self.steady_start:
            return False
        return (ordinal - self.steady_start) % self.steady_interval == 0
