"""
CliChannel — 终端适配器

职责：
    raw str → InboundEvent（字段映射）
    出站：终端模式用 gateway.ask 同步拿回复并由调用方打印；
          deliver_to_OutboundEvent 不做事（保持 Channel 契约完整，避免重复输出）

终端没有平台 SDK，parse 就是字符串进出。
"""
from __future__ import annotations

from typing import Any

from .baseChannel import BaseChannel
from agent.types.channel import InboundEvent, OutboundReply, SessionSource


class CliChannel(BaseChannel):
    name = "cli"

    def parse_to_InboundEvent(self, raw: Any) -> InboundEvent:
        """命令行输入只包含文本信息，没有其他平台字段。"""
        text = raw.strip() if isinstance(raw, str) else str(raw)
        return InboundEvent(
            text=text,
            source=SessionSource(
                channel=self.name,
                chat_id="local",
                chat_type="dm",
                user_id="local",
            ),
            channel_prompt="当前通道：本地终端 CLI。回答简洁即可。",
        )

    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:
        pass

    def start(self) -> None:
        self.status["start"] = True
        self.status["connected"] = True
