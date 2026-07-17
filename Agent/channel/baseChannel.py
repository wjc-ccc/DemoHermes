"""
Channel 基类 — 平台原始载荷 → InboundEvent；OutboundReply → 平台

在channel处实现队列解析
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
import json

from ..core.types import InboundEvent, OutboundReply, SessionSource
from ..provider import CHANNEL_CONFIG

## channel 封装逻辑 入队列 出队列 以及队列相关的一些统计属性
class BaseChannel(ABC):
    name: str = "base"
    _running: bool = False
    status: dict[str, Any] = {
        "enable": False,
        "start": False,
        "channel": "base",
        "message_count": 0,
        "inbound_event_count": 0,
        "outbound_event_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "info_count": 0,
        "debug_count": 0,
        "trace_count": 0,
    }

    def __init__(self):
        self.config = json.load(open(CHANNEL_CONFIG)) if CHANNEL_CONFIG else {}

    @abstractmethod
    def start(self) -> None:
        # TODO：启动channel，更新status
        if self.config.get(self.name, {}).get("enable"):
            self.status["enable"] = True
            self.status["start"] = True
        else:
            self.status["enable"] = False
            self.status["start"] = False

    @abstractmethod
    def stop(self) -> None:
        return None

    def get_status(self) -> dict[str, Any]:
        return self.status

    @abstractmethod
    def parse_to_InboundEvent(self, raw: Any) -> InboundEvent:
        """平台原始数据 → 统一 InboundEvent"""

    @abstractmethod
    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:
        """把统一出站发回平台。"""

    def send_gateway(self, event: InboundEvent) -> None:
        """发送消息给gateway"""
        self.status["message_count"] += 1
        self.status["inbound_event_count"] += 1
        # TODO: 成功标识 失败标识
        return None
