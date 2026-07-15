"""
WeChat Channel 骨架 — 演示「平台字段 → 统一 InDTO」只在 Channel 发生。

真正连微信 SDK 后，把 raw webhook/payload 丢进 parse_inbound 即可；
Gateway / Loop 零改动。
"""
from __future__ import annotations

from typing import Any

from .base import BaseChannel
from ..core.types import SessionSource, InboundEvent, OutboundReply, ContentPart


class WechatChannel(BaseChannel):
    name = "wechat"

    def parse_inbound(self, raw: Any) -> InboundEvent:
        """假设微信侧原始形态: {content, FromUserName, ...}"""
        if not isinstance(raw, dict):
            raise TypeError("wechat raw must be dict")
        text = raw.get("content") or raw.get("Content") or ""
        user_id = raw.get("FromUserName") or raw.get("from_user") or "unknown"
        chat_id = raw.get("chat_id") or user_id
        return InboundEvent(
            text=str(text).strip(),
            source=SessionSource(
                channel=self.name,
                chat_id=str(chat_id),
                chat_type="dm",
                user_id=str(user_id),
            ),
            content=[ContentPart(type="text", text=str(text).strip())],
            channel_prompt="当前通道：微信。语气可稍口语化。",
            metadata={"raw_keys": list(raw.keys())},
        )

    def deliver(self, reply: OutboundReply) -> None:
        # demo：尚未接 SDK，打印模拟发送
        print(f"[wechat→{reply.source.chat_id}] {reply.text}")
