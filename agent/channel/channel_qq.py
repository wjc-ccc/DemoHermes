"""
QQChannel — QQ 官方机器人适配器（Hermes qqbot/adapter.py 的精简移植）

协议（QQ Bot API v2，Discord 风格 wss 网关 + REST）：
    1. POST bots.qq.com/app/getAppAccessToken  → access_token（带过期缓存）
    2. GET  api.sgroup.qq.com/gateway          → wss 地址
    3. wss：op10 Hello → op2 Identify（intents/shard）→ READY（session_id）
            op1 心跳（带最新 seq）；op7/op9 → 重连（有会话走 op6 Resume）
            op0 Dispatch：C2C_MESSAGE_CREATE（私聊）/ GROUP_AT_MESSAGE_CREATE（群@）
    4. 出站 REST：/v2/users/{openid}/messages 或 /v2/groups/{group_openid}/messages

配置（registry.py 解析 env 后注入）：
    app_id / client_secret   必需（env: QQ_APP_ID / QQ_CLIENT_SECRET）

入站图片和文件附件缓存到 data/channel/media；回复仅发送文字。

线程模型：start() 起 daemon 线程跑 wss 收包循环（websockets.sync 同步 API），
心跳线程随连接创建；deliver 是纯同步 httpx POST，不依赖 wss 状态。

首版限制（以下是本项目的处理边界，不代表 QQ 平台配额）：
    消息类别：接收文字、图片、文件；音频/视频只生成暂不支持说明；出站仅文字。
    附件条数：对一条消息的 attachments/msg_elements 逐项处理，代码未设固定张数或文件数上限；
              多张图片按原顺序放入同一个 InboundEvent.content。
    附件大小：单个下载并落盘的附件最多 20 MiB，失败作为可见文字交给 Agent。
    媒体队列：每渠道 2 个工作线程、32 个待处理槽；满时同步处理，不静默丢弃。
    平台频率：被动回复带 msg_id；主动发送额度和接口限流按 QQ 平台响应处理，未写死数值。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
import time
from typing import Any

from ..core.types import ContentPart, InboundEvent, OutboundReply, SessionSource
from .baseChannel import BaseChannel
from .dedup import TTLDedup
from .media import download_media, save_media_bytes

logger = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL = "https://api.sgroup.qq.com/gateway"
API_BASE = "https://api.sgroup.qq.com"

# C2C 群聊私域消息 | 公域频道消息 | 频道私信
INTENTS = (1 << 25) | (1 << 30) | (1 << 12)

RECONNECT_BACKOFF = [2, 5, 10, 30, 60]  # 秒，对齐 Hermes
_MAX_RECONNECT_ATTEMPTS = 100

# 致命 close code：机器人下线/封禁，重连无意义
_FATAL_CLOSE_CODES = {4914, 4915}

_MENTION_RE = re.compile(r"<@!\S+>")


class QQChannel(BaseChannel):
    name = "qq"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        section = self.config
        self.app_id = str(section.get("app_id") or "").strip()
        self.client_secret = str(
            section.get("client_secret") or ""
        ).strip()

        self._dedup = TTLDedup()
        self._token = ""
        self._token_expire_at = 0.0
        self._session_id = ""
        self._last_seq = 0
        self._heartbeat_interval = 41.25  # 秒，Hello 下发后覆盖
        self._running = False
        self._stop_event = threading.Event()
        self._ws = None
        self._thread: threading.Thread | None = None
        self._hb_thread: threading.Thread | None = None
        self._http = None  # httpx.Client，懒建

    # ================= 生命周期 =================

    def start(self) -> None:
        self._stopping = False
        self.status["enable"] = True
        if not (self.app_id and self.client_secret):
            raise RuntimeError("qq 凭据缺失（app_id/client_secret）")
        try:
            import httpx  # noqa: F401
            import websockets.sync.client  # noqa: F401
        except ImportError:
            raise RuntimeError("qq 缺少依赖：pip install httpx websockets")
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="qq-ws", daemon=True)
        self.status["start"] = True
        try:
            self._thread.start()
        except Exception:
            self.status["start"] = False
            raise
        logger.info("qq wss 长连已启动")

    def stop(self) -> None:
        self.stop_inbound_work()
        self._running = False
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                logger.debug("qq ws 关闭异常（忽略）", exc_info=True)
        self.status["start"] = False
        self.status["connected"] = False
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)

    # ================= HTTP 薄封装（测试可替换） =================

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=15.0)
        return self._http

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        resp = self._client().post(url, json=payload, headers=headers or {})
        resp.raise_for_status()
        return resp.json()

    def _get(self, url: str, headers: dict | None = None) -> dict:
        resp = self._client().get(url, headers=headers or {})
        resp.raise_for_status()
        return resp.json()

    # ================= token / 网关 =================

    def _ensure_token(self) -> str:
        """access_token 缓存；过期前 60s 主动刷新（对齐 Hermes）。"""
        if self._token and time.time() < self._token_expire_at:
            return self._token
        data = self._post(TOKEN_URL, {"appId": self.app_id, "clientSecret": self.client_secret})
        self._token = str(data["access_token"])
        self._token_expire_at = time.time() + int(data.get("expires_in", 7200)) - 60
        return self._token

    def _gateway_url(self) -> str:
        token = self._ensure_token()
        data = self._get(GATEWAY_URL, headers={"Authorization": f"QQBot {token}"})
        return str(data["url"])

    # ================= wss 协议状态机 =================

    def _build_identify(self) -> dict:
        return {
            "op": 2,
            "d": {
                "token": f"QQBot {self._ensure_token()}",
                "intents": INTENTS,
                "shard": [0, 1],
            },
        }

    def _build_resume(self) -> dict:
        return {
            "op": 6,
            "d": {
                "token": f"QQBot {self._ensure_token()}",
                "session_id": self._session_id,
                "seq": self._last_seq,
            },
        }

    def _build_heartbeat(self) -> dict:
        return {"op": 1, "d": self._last_seq or None}

    def _next_backoff(self, attempt: int) -> float:
        return float(RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)])

    def _handle_payload(self, payload: dict) -> str | None:
        """处理一帧 wss 消息；返回动作信号：identify / reconnect / None。"""
        op = payload.get("op")
        if op == 0:  # Dispatch
            if payload.get("s"):
                self._last_seq = payload["s"]
            t = payload.get("t")
            d = payload.get("d") or {}
            if t == "READY":
                self._session_id = str(d.get("session_id", ""))
                self.status["connected"] = True
                logger.info("qq READY session_id=%s", self._session_id)
            elif t == "C2C_MESSAGE_CREATE":
                self._queue_ingest(self._c2c_to_raw(d))
            elif t == "GROUP_AT_MESSAGE_CREATE":
                self._queue_ingest(self._group_to_raw(d))
            return None
        if op == 10:  # Hello
            self._heartbeat_interval = (payload.get("d") or {}).get("heartbeat_interval", 41250) / 1000
            return "identify"
        if op in (7, 9):  # Reconnect / Invalid Session
            self.status["connected"] = False
            if op == 9 and not (payload.get("d") or {}).get("resumable", False):
                self._session_id = ""  # 不可恢复，重新 Identify
            return "reconnect"
        return None  # op11 Heartbeat ACK 等

    # ================= 收包循环 / 心跳 =================

    def _run(self) -> None:
        """wss 主循环：连接 → Hello → Identify/Resume → 收包；断线按退避阶梯重连。"""
        from websockets.sync.client import connect

        attempt = 0
        while self._running and attempt < _MAX_RECONNECT_ATTEMPTS:
            try:
                self._connect_and_listen(connect)
                attempt = 0  # 正常断开（对端要求重连）后重置退避
            except Exception as e:
                self.status["connected"] = False
                code = getattr(e, "code", None) or getattr(
                    getattr(e, "rcvd", None), "code", None
                )
                if code in _FATAL_CLOSE_CODES:
                    logger.error("qq 收到致命 close code=%s，停止重连", code)
                    break
                if code == 4004:
                    self._token = ""  # token 失效，下轮强制刷新
                logger.warning("qq wss 断开（code=%s）：%s", code, e)
            if not self._running:
                break
            delay = self._next_backoff(attempt)
            attempt += 1
            logger.info("qq %.0fs 后重连（第 %d 次）", delay, attempt)
            self._stop_event.wait(delay)
        self.status["start"] = False
        self.status["connected"] = False

    def _connect_and_listen(self, connect) -> None:
        token = self._ensure_token()
        url = self._gateway_url()
        with connect(url) as ws:
            self._ws = ws
            hello = json.loads(ws.recv(timeout=60))
            self._handle_payload(hello)  # op10 → heartbeat_interval
            if self._session_id and self._last_seq:
                ws.send(json.dumps(self._build_resume()))
            else:
                ws.send(json.dumps(self._build_identify()))
            self._start_heartbeat(ws)
            while self._running:
                payload = json.loads(ws.recv())
                if self._handle_payload(payload) == "reconnect":
                    return

    def _start_heartbeat(self, ws) -> None:
        def _beat() -> None:
            while self._running and self._ws is ws:
                if self._stop_event.wait(max(self._heartbeat_interval * 0.8, 1.0)):
                    return
                try:
                    ws.send(json.dumps(self._build_heartbeat()))
                except Exception:
                    return  # 连接已断，交给主循环重连

        self._hb_thread = threading.Thread(target=_beat, name="qq-heartbeat", daemon=True)
        self._hb_thread.start()

    # ================= 入站归一化 =================

    def _c2c_to_raw(self, d: dict) -> dict:
        openid = str((d.get("author") or {}).get("user_openid", ""))
        return {
            "message_id": str(d.get("id", "")),
            "chat_id": openid,
            "chat_type": "dm",
            "user_id": openid,
            "text": str(d.get("content", "")),
            "attachments": self._attachments(d),
        }

    def _group_to_raw(self, d: dict) -> dict:
        return {
            "message_id": str(d.get("id", "")),
            "chat_id": str(d.get("group_openid", "")),
            "chat_type": "group",
            "user_id": str((d.get("author") or {}).get("member_openid", "")),
            "text": str(d.get("content", "")),
            "attachments": self._attachments(d),
        }

    @staticmethod
    def _attachments(d: dict) -> list[dict]:
        result = list(d.get("attachments") or [])
        for element in d.get("msg_elements") or []:
            result.extend(element.get("attachments") or [])
        return result

    def _queue_ingest(self, raw: dict) -> None:
        if raw.get("attachments"):
            self.submit_inbound_work(self._ingest, raw)
        else:
            self._ingest(raw)

    def should_accept(self, raw: dict) -> bool:
        return not self._dedup.is_dup(str(raw.get("message_id", "")))

    def _ingest(self, raw: dict) -> None:
        try:
            if not self.should_accept(raw):
                return
            self.send_gateway(self.parse_to_InboundEvent(raw))
        except Exception:
            self.status["error_count"] += 1
            logger.exception("qq 入站消息处理失败")

    def parse_to_InboundEvent(self, raw: Any) -> InboundEvent:
        if not isinstance(raw, dict):
            raise TypeError(f"qq raw 必须是 dict，得到 {type(raw)}")
        chat_type = "group" if raw.get("chat_type") == "group" else "dm"
        text = _MENTION_RE.sub("", str(raw.get("text", ""))).strip()
        parts: list[ContentPart] = []
        for index, attachment in enumerate(raw.get("attachments") or [], start=1):
            try:
                url = str(attachment.get("url") or "")
                if url.startswith("//"):
                    url = "https:" + url
                provided_name = str(attachment.get("filename") or "")
                mime = str(attachment.get("content_type") or mimetypes.guess_type(provided_name)[0] or "application/octet-stream")
                name = provided_name or f"attachment-{index}{mimetypes.guess_extension(mime.split(';', 1)[0]) or '.bin'}"
                if mime.startswith(("audio/", "video/")):
                    text = "\n".join(filter(None, [text, f"[暂不支持音视频附件: {name}]"]))
                    continue
                data = download_media(url, allowed_domains=("qq.com", "qq.com.cn", "gtimg.cn", "qpic.cn"))
                parts.append(save_media_bytes(data, channel=self.name, file_name=name, mime_type=mime))
            except Exception as exc:
                self.status["error_count"] += 1
                text = "\n".join(filter(None, [text, f"[附件读取失败: {exc}]"]))
        prompt = (
            "当前通道：QQ 私聊。回答简洁即可。"
            if chat_type == "dm"
            else "当前通道：QQ 群聊。回答简洁，不要用过多 Markdown 格式。"
        )
        return InboundEvent(
            text=text,
            content=parts,
            source=SessionSource(
                channel=self.name,
                chat_id=str(raw.get("chat_id", "")),
                chat_type=chat_type,
                user_id=str(raw.get("user_id", "")),
                reply_to_message_id=raw.get("message_id") or None,
            ),
            channel_prompt=prompt,
            metadata={"message_id": raw.get("message_id", "")},
        )

    # ================= 出站 =================

    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:
        """REST 发消息：dm → /v2/users/{openid}，group → /v2/groups/{group_openid}。"""
        token = self._ensure_token()
        chat_id = reply.source.chat_id
        if reply.source.chat_type == "group":
            url = f"{API_BASE}/v2/groups/{chat_id}/messages"
        else:
            url = f"{API_BASE}/v2/users/{chat_id}/messages"
        payload: dict[str, Any] = {"content": reply.text, "msg_type": 0}
        msg_id = reply.reply_to or reply.source.reply_to_message_id
        if msg_id:
            payload["msg_id"] = msg_id  # 被动回复带 msg_id，免主动消息频次限制
        result = self._post(url, payload, headers={"Authorization": f"QQBot {token}"})
        if result.get("code") not in (None, 0):
            raise RuntimeError(f"qq 出站失败 code={result.get('code')}")
