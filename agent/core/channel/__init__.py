"""
Agent/channel 包 — 平台适配层

每个 Channel 负责：平台原始载荷 ⇄ 统一 DTO（InboundEvent / OutboundReply）。
平台差异停在这里：字段在生成 raw dict 时归一（chat_type 已是 dm / group），
之后走 BaseChannel.ingest（计数、去重、准入、附件进线程池）再 parse。

平台 channel（feishu/qq/wechat）由 registry.autostart_channels 按配置自动启用：
registry.json 未显式 "enable": false 且 .env 凭据填齐即连接。
名字、env 变量名、依赖、提示词都声明在 channel 类属性上，registry 只读取。
CLI 与 Frontier 不走注册表，由 main 直接注册；回复经 gateway.ask 同步返回。

新增平台：继承 BaseChannel，实现 start / parse_to_InboundEvent / deliver_to_OutboundEvent，
按需覆写 _has_media、should_accept、_close_connection，再加入 registry.PLATFORM_CHANNELS。
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
