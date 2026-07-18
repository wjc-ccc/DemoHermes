"""
BaseChannel — Channel 基类：平台原始载荷 → InboundEvent；OutboundReply → 平台

职责边界（全项目约定）：
    - 平台字段解析只发生在 Channel（parse_to_InboundEvent）
    - 出站平台投递只发生在 Channel（deliver_to_OutboundEvent）
    - Channel 不碰 Session / Loop / 模型；拿到事件后 send_gateway 交给总线

与 Gateway 的关系：
    Gateway.register_channel(channel) 时会回调 bind_gateway(self)，
    之后 Channel 通过 send_gateway(event) 把入站事件送进 MessageBus。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from ..core.types import InboundEvent, OutboundReply
from ..provider import CHANNEL_CONFIG


class BaseChannel(ABC):
    name: str = "base"

    def __init__(self) -> None:
        # 渠道配置（CHANNEL_CONFIG 指向的 JSON，可为空）
        self.config: dict = {}
        if CHANNEL_CONFIG:
            try:
                with open(CHANNEL_CONFIG, encoding="utf-8") as f:
                    self.config = json.load(f)
            except Exception:
                self.config = {}

        self.status: dict[str, Any] = {
            "enable": False,           # 配置里是否启用
            "start": False,            # 是否已启动
            "channel": self.name,
            "message_count": 0,        # 入站消息总数
            "inbound_event_count": 0,  # 已送进 gateway 的事件数
            "outbound_event_count": 0, # 已投递的回复数（gateway 出站时累加）
            "error_count": 0,
        }
        self._gateway = None  # bind_gateway 后可用

    # ---- 与 Gateway 绑定 ----
    def bind_gateway(self, gateway) -> None:
        """由 Gateway.register_channel 回调；绑定后 send_gateway 才有去处。"""
        self._gateway = gateway

    def send_gateway(self, event: InboundEvent) -> None:
        """把解析好的入站事件交给 Gateway（进 MessageBus 的 in_dto）。"""
        self.status["message_count"] += 1
        self.status["inbound_event_count"] += 1
        if self._gateway is None:
            self.status["error_count"] += 1
            raise RuntimeError(f"channel={self.name} 尚未 bind_gateway，无法发送事件")
        self._gateway.submit(event)

    # ---- 生命周期 ----
    @abstractmethod
    def start(self) -> None:
        """启动渠道（读配置、连平台）；无平台可连的渠道置 start=True 即可。"""

    @abstractmethod
    def stop(self) -> None:
        """停止渠道，释放平台连接。"""

    def get_status(self) -> dict[str, Any]:
        return self.status

    # ---- I/O 归一化（子类必须实现）----
    @abstractmethod
    def parse_to_InboundEvent(self, raw: Any) -> InboundEvent:
        """平台原始数据 → 统一 InboundEvent。"""

    @abstractmethod
    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:
        """统一出站 → 发回平台。"""
