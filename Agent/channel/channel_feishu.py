"""
FeishuChannel — 飞书长连接适配器（Hermes feishu.py 的精简移植）

连接方式（对齐 Hermes，靠官方 lark-oapi SDK）：
    入站：lark_oapi.ws.Client websocket 长连（SDK 自带自动重连），
          事件回调把 lark event 提取成纯 dict → parse_to_InboundEvent → send_gateway
    出站：lark REST API（im.v1.message.reply 优先，create 兜底，统一 chat_id 寻址）

配置（registry.json 的 "feishu" 节 + env 兜底）：
    app_id / app_secret   必需（env: FEISHU_APP_ID / FEISHU_APP_SECRET）
    domain                feishu（默认）| lark（海外版）
    require_mention       群聊是否必须 @ 机器人才响应（默认 true）
    bot_open_id           群聊 @ 识别用；缺省时 start() 自动水合

SDK 全程懒加载（import 在 start/deliver 内），离线跑 parse 测试不需要装依赖。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from ..core.types import InboundEvent, OutboundReply, SessionSource
from .baseChannel import BaseChannel
from .dedup import TTLDedup

logger = logging.getLogger(__name__)


class FeishuChannel(BaseChannel):
    name = "feishu"

    def __init__(self) -> None:
        super().__init__()
        section = (self.config.get(self.name) or {}) if isinstance(self.config, dict) else {}
        self.app_id = str(section.get("app_id") or os.getenv("FEISHU_APP_ID", "")).strip()
        self.app_secret = str(section.get("app_secret") or os.getenv("FEISHU_APP_SECRET", "")).strip()
        self.domain = str(section.get("domain") or os.getenv("FEISHU_DOMAIN", "feishu")).strip()
        self.require_mention = bool(section.get("require_mention", True))
        self._bot_open_id = str(
            section.get("bot_open_id") or os.getenv("FEISHU_BOT_OPEN_ID", "")
        ).strip()

        self._dedup = TTLDedup()
        self._client = None       # lark.Client（REST）
        self._ws_client = None    # lark_oapi.ws.Client（长连）
        self._ws_thread: threading.Thread | None = None

    # ================= 生命周期 =================

    def start(self) -> None:
        """建 REST client + 事件分发器 + ws 长连（daemon 线程，SDK 自动重连）。"""
        self.status["enable"] = True
        if not (self.app_id and self.app_secret):
            logger.warning("feishu 凭据缺失（app_id/app_secret），无法启动")
            return
        try:
            import lark_oapi as lark
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as FeishuWSClient
        except ImportError:
            logger.warning("feishu 缺少依赖：pip install lark-oapi")
            return

        domain = lark.LARK_DOMAIN if self.domain == "lark" else lark.FEISHU_DOMAIN
        self._client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .domain(domain)
            .build()
        )
        handler = (
            EventDispatcherHandler.builder("", "")  # ws 模式无需 encrypt_key/verification_token
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        self._ws_client = FeishuWSClient(
            self.app_id, self.app_secret, event_handler=handler, domain=domain
        )
        self._hydrate_bot_identity()
        self._ws_thread = threading.Thread(
            target=self._ws_client.start, name="feishu-ws", daemon=True
        )
        self._ws_thread.start()
        self.status["start"] = True
        logger.info("feishu ws 长连已启动（domain=%s）", self.domain)

    def stop(self) -> None:
        ws = self._ws_client
        if ws is not None:
            for attr in ("stop", "_disconnect"):  # SDK 版本的退出入口不一致，尽力而为
                fn = getattr(ws, attr, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        logger.debug("feishu ws 断开异常（忽略）", exc_info=True)
                    break
        self.status["start"] = False

    def _hydrate_bot_identity(self) -> None:
        """调 /bot/v3/info 拿 bot open_id（群聊 @ 识别用）；失败不阻塞启动。"""
        if self._bot_open_id or self._client is None:
            return
        try:
            from lark_oapi.core import AccessTokenType, HttpMethod
            from lark_oapi.core.model import BaseRequest

            req = (
                BaseRequest.builder()
                .http_method(HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({AccessTokenType.TENANT})
                .build()
            )
            resp = self._client.request(req)
            data = json.loads(resp.raw.content.decode("utf-8"))
            self._bot_open_id = str(data.get("bot", {}).get("open_id") or "")
            if self._bot_open_id:
                logger.info("feishu bot open_id 水合成功: %s", self._bot_open_id)
        except Exception:
            logger.warning("feishu bot 身份水合失败，群聊 @ 过滤将放行所有消息", exc_info=True)

    # ================= 入站 =================

    def _on_message(self, data: Any) -> None:
        """lark ws 事件回调（SDK 线程）：lark 对象 → 纯 dict → 准入 → 归一化 → 总线。"""
        try:
            raw = self.event_to_raw(data)
            if not self.should_accept(raw):
                return
            self.send_gateway(self.parse_to_InboundEvent(raw))
        except Exception:
            self.status["error_count"] += 1
            logger.exception("feishu 入站消息处理失败")

    def event_to_raw(self, data: Any) -> dict:
        """lark P2ImMessageReceiveV1 对象 → 纯 dict（平台对象处理集中在这一个方法）。"""
        event = data.event
        message = event.message
        sender = event.sender

        try:
            content = json.loads(message.content or "{}")
        except (TypeError, json.JSONDecodeError):
            content = {}
        if message.message_type == "text":
            text = str(content.get("text", ""))
        else:
            text = f"[暂不支持的消息类型: {message.message_type}]"

        mentions = []
        for m in message.mentions or []:
            mentions.append({
                "open_id": getattr(getattr(m, "id", None), "open_id", "") or "",
                "name": getattr(m, "name", "") or "",
                "key": getattr(m, "key", "") or "",
            })

        return {
            "message_id": message.message_id or "",
            "chat_id": message.chat_id or "",
            "chat_type": message.chat_type or "p2p",
            "user_id": getattr(getattr(sender, "sender_id", None), "open_id", "") or "",
            "user_name": "",
            "message_type": message.message_type or "text",
            "text": text,
            "mentions": mentions,
        }

    def should_accept(self, raw: dict) -> bool:
        """准入：message_id 去重 + 群聊 require_mention 过滤。"""
        if self._dedup.is_dup(str(raw.get("message_id", ""))):
            return False
        if raw.get("chat_type") == "group" and self.require_mention and self._bot_open_id:
            mentioned = any(
                m.get("open_id") == self._bot_open_id for m in raw.get("mentions", [])
            )
            if not mentioned:
                return False
        return True

    def parse_to_InboundEvent(self, raw: Any) -> InboundEvent:
        """纯 dict → InboundEvent；剥 @ 前缀等平台杂务在这里消化。"""
        if not isinstance(raw, dict):
            raise TypeError(f"feishu raw 必须是 dict（event_to_raw 的产物），得到 {type(raw)}")
        chat_type = "dm" if raw.get("chat_type") == "p2p" else "group"
        text = str(raw.get("text", ""))
        for m in raw.get("mentions", []):
            key = m.get("key")
            if key:
                text = text.replace(key, "")
        text = text.strip()

        prompt = (
            "当前通道：飞书私聊。回答简洁即可。"
            if chat_type == "dm"
            else "当前通道：飞书群聊。回答简洁，不要用过多 Markdown 格式。"
        )
        return InboundEvent(
            text=text,
            source=SessionSource(
                channel=self.name,
                chat_id=str(raw.get("chat_id", "")),
                chat_type=chat_type,
                user_id=str(raw.get("user_id", "")),
                user_name=raw.get("user_name") or None,
                reply_to_message_id=raw.get("message_id") or None,
            ),
            channel_prompt=prompt,
            metadata={
                "message_id": raw.get("message_id", ""),
                "message_type": raw.get("message_type", "text"),
            },
        )

    # ================= 出站 =================

    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:
        """reply 优先（挂回原消息），失败/无 reply_to 则 create（chat_id 寻址 dm/group 通吃）。"""
        if self._client is None:
            self.status["error_count"] += 1
            logger.warning("feishu 未启动（无 REST client），出站丢弃: %s", reply.text[:60])
            return
        try:
            if self._send_text(reply):
                self.status["outbound_event_count"] += 1
            else:
                self.status["error_count"] += 1
                logger.warning("feishu 出站失败: %s", reply.text[:60])
        except Exception:
            self.status["error_count"] += 1
            logger.exception("feishu 出站异常")

    def _send_text(self, reply: OutboundReply) -> bool:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        content = json.dumps({"text": reply.text}, ensure_ascii=False)

        message_id = reply.reply_to or reply.source.reply_to_message_id
        if message_id:
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(content)
                    .build()
                )
                .build()
            )
            resp = self._client.im.v1.message.reply(req)
            if resp.success():
                return True
            logger.warning("feishu reply 失败转 create: %s", getattr(resp, "msg", ""))

        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(reply.source.chat_id)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message.create(req)
        return bool(resp.success())
