"""
Channel 基类 — 平台原始载荷 → InboundEvent；OutboundReply → 平台

解析（字段映射）只发生在这里，不进 Loop / Gateway。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.types import InboundEvent, OutboundReply, SessionSource, ContentPart


class BaseChannel(ABC):
    name: str = "base"

    @abstractmethod
    def parse_inbound(self, raw: Any) -> InboundEvent:
        """平台原始数据 → 统一 InboundEvent（含 SessionSource）。"""

    @abstractmethod
    def deliver(self, reply: OutboundReply) -> None:
        """把统一出站发回平台。"""

    def make_source(self, **kwargs) -> SessionSource:
        return SessionSource(channel=self.name, **kwargs)
