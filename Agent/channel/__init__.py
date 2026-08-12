"""
Agent/channel 包 — 平台适配层

每个 Channel 负责：平台原始载荷 ⇄ 统一 DTO（InboundEvent / OutboundReply）。
新增平台只需继承 BaseChannel 实现 parse/deliver，Gateway 与 Loop 零改动。

平台 channel（feishu/qq/wechat）由 registry.autostart_channels 按配置自动启用：
registry.json 未显式 "enable": false 且凭据（JSON 同名 key 或 env）填齐即连接。
"""
from .baseChannel import BaseChannel
from .channel_cli import CliChannel
from .channel_feishu import FeishuChannel
from .channel_frontier import FrontierChannel
from .channel_qq import QQChannel
from .channel_wechat import WechatChannel
from .registry import autostart_channels

__all__ = [
    "BaseChannel",
    "CliChannel",
    "FrontierChannel",
    "FeishuChannel",
    "QQChannel",
    "WechatChannel",
    "autostart_channels",
]
