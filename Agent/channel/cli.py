"""
CLI Channel — 终端适配器

职责：
    raw str / dict → InboundEvent（字段映射）
    OutboundReply → print

示例：若将来微信 payload 是 {"content": "..."}，
在 WechatChannel.parse_inbound 里写成 text=payload["content"]，与 CLI 同契约。
"""
from __future__ import annotations

from typing import Any

from .base import BaseChannel
from ..core.types import SessionSource, InboundEvent, OutboundReply, ContentPart


class CliChannel(BaseChannel):
    name = "cli"

    def __init__(self, chat_id: str = "local", user_id: str = "local", *, quiet: bool = False):
        self.chat_id = chat_id
        self.user_id = user_id
        self.quiet = quiet  # ask() 同步模式时可关掉自动 print，由 CLI 自己打印

    def parse_inbound(self, raw: Any) -> InboundEvent:
        """
        接受：
            - str：「你好」
            - dict：{"text": "..."} 或兼容 {"content": "..."}（演示字段映射）
        """
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, dict):
            # 故意支持 content→text，模拟微信字段名差异在 Channel 内消化
            text = raw.get("text") or raw.get("content") or ""
        else:
            text = str(raw)

        return InboundEvent(
            text=text.strip(),
            source=SessionSource(
                channel=self.name,
                chat_id=self.chat_id,
                chat_type="dm",
                user_id=self.user_id,
            ),
            content=[ContentPart(type="text", text=text.strip())] if text.strip() else [],
            channel_prompt="当前通道：本地终端 CLI。回答简洁即可。",
        )

    # 兼容旧名
    def to_inbound(self, raw_text: str) -> InboundEvent:
        return self.parse_inbound(raw_text)

    def deliver(self, reply: OutboundReply) -> None:
        if self.quiet:
            return
        prefix = "ai  > "
        if not reply.completed and reply.error:
            print(f"{prefix}[error] {reply.error}")
        else:
            print(f"{prefix}{reply.text}")
