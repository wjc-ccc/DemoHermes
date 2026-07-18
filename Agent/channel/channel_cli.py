"""
CliChannel — 终端适配器

职责：
    raw str → InboundEvent（字段映射）
    OutboundReply → print

终端没有平台 SDK，parse/deliver 就是字符串进出。
"""
from __future__ import annotations

from typing import Any

from .baseChannel import BaseChannel
from ..core.types import InboundEvent, OutboundReply, SessionSource


class CliChannel(BaseChannel):
    name = "cli"

    def __init__(self, chat_id: str = "local", user_id: str = "local", *, quiet: bool = False):
        super().__init__()
        self.chat_id = chat_id
        self.user_id = user_id
        self.quiet = quiet  # 测试时不打印

    def parse_to_InboundEvent(self, raw: Any) -> InboundEvent:
        """命令行输入只包含文本信息，没有其他平台字段。"""
        text = raw.strip() if isinstance(raw, str) else str(raw)
        return InboundEvent(
            text=text,
            source=SessionSource(
                channel=self.name,
                chat_id=self.chat_id,
                chat_type="dm",
                user_id=self.user_id,
            ),
            channel_prompt="当前通道：本地终端 CLI。回答简洁即可。",
        )

    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:
        if self.quiet:
            return
        prefix = "ai  > "
        error = (reply.metadata or {}).get("error")
        if error:
            print(f"{prefix}[error] {error}")
        else:
            print(f"{prefix}{reply.text}")

    def start(self) -> None:
        self.status["enable"] = True
        self.status["start"] = True

    def stop(self) -> None:
        self.status["start"] = False
