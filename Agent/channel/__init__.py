"""
Agent/channel 包 — 平台适配层

每个 Channel 负责：平台原始载荷 ⇄ 统一 DTO（InboundEvent / OutboundReply）。
新增平台只需继承 BaseChannel 实现 parse/deliver，Gateway 与 Loop 零改动。
"""
from .baseChannel import BaseChannel
from .channel_cli import CliChannel
from .channel_frontier import FrontierChannel

__all__ = ["BaseChannel", "CliChannel", "FrontierChannel"]
