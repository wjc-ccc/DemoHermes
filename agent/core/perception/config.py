"""感知侧尚未放进 PerceptionConfig / AdmissionConfig 的配置。

压缩比例和准入打分已经并入对应的 Config。这里只留调用前估算和判官传输。
"""
from __future__ import annotations

from dataclasses import dataclass


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须为正整数")


def _non_negative_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是大于等于 0 的整数")


@dataclass(frozen=True)
class JudgeConfig:
    """判官传输配置。不设置时沿用环境变量里的超时和批大小。"""

    timeout_seconds: float = 6.0
    batch_size: int = 4
    max_workers: int = 4

    def __post_init__(self) -> None:
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是正数")
        _positive_int("batch_size", self.batch_size)
        _positive_int("max_workers", self.max_workers)


@dataclass(frozen=True)
class TokenEstimateConfig:
    """没有真实 tokenizer 时，调用前预算使用的保守估算参数。

    每条消息按 UTF-8 字节计，再加固定开销；图片按张加固定成本。
    这是发请求前的上限，不是服务端返回的 usage。
    """

    message_overhead_bytes: int = 16
    image_token_cost: int = 8192

    def __post_init__(self) -> None:
        _non_negative_int("message_overhead_bytes", self.message_overhead_bytes)
        _non_negative_int("image_token_cost", self.image_token_cost)
