"""MemoryProvider - 外部记忆系统唯一外部 seam（7.23 方案 §2 Seam D / §7）。

同时最多一个，由 MemoryManager 构造注入（§7.1）。测试允许使用 Fake；
不得 mock 自己的 HotStore/DailyStore/Manager。

recall/build_context 失败或超时由 Manager 降级为 diagnostic，不阻断主对话（§7.2）。
capabilities（recall/read/add/replace/remove/search/sync_turn）在子类声明，
Manager 路由前检查，不支持的能力返回 provider_capability_unsupported（后续 Slice 测试驱动）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .types import (
    ProviderContribution,
    ProviderMutation,
    ProviderMutationResult,
    ProviderReadRequest,
    ProviderRecallRequest,
    TurnEnvelope,
)


class MemoryProvider(ABC):
    """外部记忆 Provider 抽象基类（§2 Seam D）。

    capabilities 声明支持的能力子集
    （recall/read/add/replace/remove/search/sync_turn），Manager 路由前检查，
    不支持的能力返回 provider_capability_unsupported（§7.1）。
    """

    name: str
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    def recall(self, request: ProviderRecallRequest) -> ProviderContribution:
        """针对当前 query 返回相关上下文贡献；失败/超时由 Manager 捕获降级。"""

    def apply(self, mutation: ProviderMutation) -> ProviderMutationResult:
        """显式 add/replace/remove；默认未实现，子类按 capability 覆盖（§2 Seam D）。"""
        raise NotImplementedError(f"{self.name} 未实现 apply")

    def read(self, request: ProviderReadRequest):
        """读取一条外部记忆；默认未实现，子类按 capability 覆盖。"""
        raise NotImplementedError(f"{self.name} 未实现 read")

    def sync_turn(self, turn: TurnEnvelope) -> None:
        """可选；完成 turn 后同步，默认 no-op（§2 Seam D / §7.2）。"""
        return None
