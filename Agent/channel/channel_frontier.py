"""
FrontierChannel — 可视化前端适配器

前端（frontier/）经 HTTP API 与后端交互：
    入站：POST /api/chat 的 JSON body 就是 raw，这里归一化为 InboundEvent
    出站：HTTP 层用 gateway.ask 同步拿回复直接返回；
          deliver_to_OutboundEvent 仅做计数与日志（保持 Channel 契约完整）

raw 约定（dict）：
    {"text": "...", "chat_id": "web", "user_id": "web-user"}
"""
from __future__ import annotations

import logging
from typing import Any

from .baseChannel import BaseChannel
from ..core.types import InboundEvent, OutboundReply, SessionSource

logger = logging.getLogger(__name__)


class FrontierChannel(BaseChannel):
    name = "frontier"

    def parse_to_InboundEvent(self, raw: Any) -> InboundEvent:
        if isinstance(raw, dict):
            text = str(raw.get("text", "")).strip()
            chat_id = str(raw.get("chat_id") or "web")
            user_id = str(raw.get("user_id") or "web-user")
        else:
            # 兼容直接传字符串
            text, chat_id, user_id = str(raw).strip(), "web", "web-user"
        return InboundEvent(
            text=text,
            source=SessionSource(
                channel=self.name,
                chat_id=chat_id,
                chat_type="dm",
                user_id=user_id,
            ),
            channel_prompt="当前通道：Web 可视化前端。回答可使用 Markdown 格式。",
        )

    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:
        # HTTP 同步链路已由 ask() 把回复带回给前端；这里只记账
        error = (reply.metadata or {}).get("error")
        if error:
            self.status["error_count"] += 1
            logger.warning("frontier 出站携带错误: %s", error)
        else:
            logger.info("frontier 出站: %s", reply.text[:80])

    def start(self) -> None:
        self.status["enable"] = True
        self.status["start"] = True

    def stop(self) -> None:
        self.status["start"] = False
