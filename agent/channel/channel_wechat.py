"""
WechatChannel — 个人微信适配器（Hermes weixin.py 的精简移植，腾讯 iLink Bot API）

协议（iLink Bot API，HTTP 长轮询而非 websocket）：
    入站：POST {base}/ilink/bot/getupdates，35s 长轮询，get_updates_buf 游标推进
    出站：POST {base}/ilink/bot/sendmessage，带回该会话最新的 context_token
    鉴权：Authorization Bearer {token} + AuthorizationType: ilink_bot_token

配置（registry.py 解析 env 后注入）：
    token                必需（env: WEIXIN_TOKEN），可运行 scripts/wechat_qr_login.py 扫码获取
    account_id           可选（env: WEIXIN_ACCOUNT_ID），协议请求不使用，仅用于日志标识
    base_url             默认 https://ilinkai.weixin.qq.com

精简边界：文字回复；图片和文件入站经 AES-128-ECB CDN 解密。
轮询线程里先拆字段、记下 context_token、再去重，然后才把带附件的消息交线程池；
因此 _save_state 时 token 已经完整。token 只留在 channel 内（按 chat_id），
不放进 InboundEvent。游标与 token 持久化到 data/channel/wechat_state.json
（不按账号分文件），重启不丢进度；出站只读这份最新值。

首版限制（以下是本项目的处理边界，不代表微信 iLink 平台配额）：
    消息类别：接收文字、图片、文件；音频/视频不解析；出站仅文字。
    附件条数：逐项处理同一 item_list 内的图片和文件，代码未设固定张数或文件数上限；
              多张图片按原顺序放入同一个 InboundEvent.content。
    附件大小：单个 CDN 下载和落盘附件最多 20 MiB，失败作为可见文字交给 Agent。
    媒体队列：每渠道 2 个工作线程、32 个待处理槽；满时同步处理，不静默丢弃。
    文字出站：每个 sendmessage 最多 2000 字符，超长自动拆成多条（本项目约定）。
    平台频率：iLink 服务端的消息/媒体额度未在此处写死，以接口返回为准。
"""
from __future__ import annotations

import json
import logging
import base64
import random
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent.types.conversation import ContentPart
from agent.types.channel import InboundEvent, OutboundReply, SessionSource
from .baseChannel import BaseChannel
from .media import download_media, save_media_bytes

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
MEDIA_DOMAINS = ("weixin.qq.com", "weixin.com", "qq.com", "qpic.cn", "qlogo.cn")

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_FILE = 4
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
ILINK_APP_HEADERS = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)}

POLL_TIMEOUT = 40.0          # 长轮询 35s + 5s 余量
MAX_TEXT_CHUNK = 2000        # iLink 单条文本上限
SESSION_EXPIRED_ERRCODE = -14
SESSION_EXPIRED_SLEEP = 600.0  # 会话过期：睡 10 分钟（对齐 Hermes）


class WechatChannel(BaseChannel):
    name = "wechat"
    label = "微信（iLink）"
    env_map = {"token": "WEIXIN_TOKEN"}
    optional_env_map = {"account_id": "WEIXIN_ACCOUNT_ID", "base_url": "WEIXIN_BASE_URL"}
    deps = ("httpx", "cryptography")
    install_hint = "pip install httpx cryptography"
    _http_timeout = None  # 长轮询要 40s，超时逐请求指定
    prompts = {
        "dm": "当前通道：微信私聊。回答简洁口语化，不要用 Markdown 格式。",
        "group": "当前通道：微信群聊。回答简洁口语化，不要用 Markdown 格式。",
    }

    def __init__(self, config: dict | None = None, *, state_path: Path | None = None) -> None:
        super().__init__(config)
        self.token = str(self.config.get("token") or "").strip()
        self.account_id = str(self.config.get("account_id") or "").strip()
        self.base_url = str(self.config.get("base_url") or ILINK_BASE_URL).strip().rstrip("/")
        self.cdn_base_url = str(self.config.get("cdn_base_url") or "https://novac2c.cdn.weixin.qq.com/c2c").rstrip("/")

        self._sync_buf = ""
        self._context_tokens: dict[str, str] = {}  # chat_id → 最新 context_token
        self._state_path = state_path or (
            Path(__file__).resolve().parents[2] / "data" / "channel" / "wechat_state.json"
        )
        self._load_state()

    # ================= 生命周期 =================

    def start(self) -> None:
        self._require_credentials()
        self._start_worker(self._poll_loop, "wechat-poll")
        logger.info("wechat iLink 长轮询已启动（account=%s）", self.account_id or "未配置")

    # ================= HTTP 薄封装（测试可替换） =================

    def _headers(self, body: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Content-Length": str(len(body.encode("utf-8"))),
            "X-WECHAT-UIN": str(random.getrandbits(32)),
            **ILINK_APP_HEADERS,
            "Authorization": f"Bearer {self.token}",
        }

    def _post(self, endpoint: str, payload: dict, timeout: float = 15.0) -> dict:
        body = json.dumps(
            {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
            ensure_ascii=False,
        )
        resp = self._http_client().post(
            f"{self.base_url}/{endpoint}",
            content=body.encode("utf-8"),
            headers=self._headers(body),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ================= 长轮询 =================

    def _poll_loop(self) -> None:
        failures = 0
        while not self._stop_event.is_set():
            try:
                resp = self._poll_once()
                expired = self._is_session_expired(resp)
                self.status["connected"] = resp.get("ret", 0) == 0 and not expired
                failures = 0
                if expired:
                    logger.error("wechat 会话过期（errcode=-14），%.0fs 后重试", SESSION_EXPIRED_SLEEP)
                    self._stop_event.wait(SESSION_EXPIRED_SLEEP)
            except Exception:
                self.status["connected"] = False
                failures += 1
                delay = 2.0 if failures < 3 else 30.0  # 连续失败退避（对齐 Hermes）
                logger.warning("wechat 轮询失败（第 %d 次），%.0fs 后重试", failures, delay, exc_info=True)
                self._stop_event.wait(delay)
        self.status["start"] = False
        self.status["connected"] = False

    def _poll_once(self) -> dict:
        """一轮长轮询：推进游标 → 逐条入站 → 落盘状态；超时当空响应。

        字段提取、context_token 更新和去重都在本线程完成，_save_state 时状态已完整；
        只有附件下载与解析交给线程池。
        """
        import httpx

        try:
            resp = self._post(
                EP_GET_UPDATES, {"get_updates_buf": self._sync_buf}, timeout=POLL_TIMEOUT
            )
        except httpx.ReadTimeout:
            return {"ret": 0, "msgs": [], "get_updates_buf": self._sync_buf}
        new_buf = str(resp.get("get_updates_buf") or "")
        if not self._is_session_expired(resp) and (resp.get("ret", 0) != 0 or resp.get("errcode", 0) != 0):
            raise RuntimeError(f"wechat getupdates 失败 ret={resp.get('ret')} errcode={resp.get('errcode')}")
        if new_buf:
            self._sync_buf = new_buf
        for message in resp.get("msgs") or []:
            self._ingest_message(message)
        if resp.get("msgs"):
            self._save_state()
        return resp

    def _is_session_expired(self, resp: dict) -> bool:
        return resp.get("errcode") == SESSION_EXPIRED_ERRCODE or resp.get("ret") == SESSION_EXPIRED_ERRCODE

    # ================= 入站归一化 =================

    def _ingest_message(self, message: dict) -> None:
        try:
            raw = self._message_to_raw(message)
        except Exception:
            self.status["error_count"] += 1
            logger.exception("wechat 入站消息解析失败")
            return
        if raw is not None:
            self.ingest(raw)

    def _message_to_raw(self, message: dict) -> dict | None:
        """iLink message → 纯 dict；非文本/空文本返回 None。"""
        sender = str(message.get("from_user_id") or "").strip()
        if not sender:
            return None
        text = ""
        media_items = []
        for item in message.get("item_list") or []:
            if item.get("type") == ITEM_TEXT:
                text = str((item.get("text_item") or {}).get("text") or "")
            elif item.get("type") in {ITEM_IMAGE, ITEM_FILE}:
                media_items.append(item)
        text = text.strip()
        if not text and not media_items:
            return None

        room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
        chat_type = "group" if room_id else "dm"
        chat_id = room_id or sender

        # 出站只认每个会话最新的 context_token，在这里记下并随状态文件持久化
        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._context_tokens[chat_id] = context_token

        return {
            "message_id": str(message.get("message_id") or ""),
            "chat_id": chat_id,
            "chat_type": chat_type,
            "user_id": sender,
            "text": text,
            "media_items": media_items,
        }

    def _has_media(self, raw: dict) -> bool:
        return bool(raw.get("media_items"))

    def parse_to_InboundEvent(self, raw: dict) -> InboundEvent:
        text, parts = self._collect_attachments(
            str(raw.get("text", "")), raw.get("media_items") or [], self._load_attachment,
        )
        return InboundEvent(
            text=text,
            content=parts,
            source=SessionSource(
                channel=self.name,
                chat_id=str(raw.get("chat_id", "")),
                chat_type=raw["chat_type"],
                user_id=str(raw.get("user_id", "")),
                reply_to_message_id=raw.get("message_id") or None,
            ),
            channel_prompt=self.prompts[raw["chat_type"]],
        )

    def _load_attachment(self, item: dict, index: int) -> ContentPart:
        is_image = item.get("type") == ITEM_IMAGE
        detail = item.get("image_item" if is_image else "file_item") or {}
        media = detail.get("media") or {}
        encrypted_param = str(media.get("encrypt_query_param") or "")
        full_url = str(media.get("full_url") or "")
        if encrypted_param:
            url = f"{self.cdn_base_url}/download?encrypted_query_param={quote(encrypted_param, safe='')}"
        elif full_url:
            url = full_url
        else:
            raise ValueError("缺少微信附件地址")
        data = download_media(url, client=self._http_client(), allowed_domains=MEDIA_DOMAINS)
        key_value = str(detail.get("aeskey") or media.get("aes_key") or "")
        if key_value:
            try:
                key = bytes.fromhex(key_value)
            except ValueError:
                decoded = base64.b64decode(key_value)
                key = bytes.fromhex(decoded.decode("ascii")) if len(decoded) == 32 else decoded
            if len(key) != 16 or len(data) % 16:
                raise ValueError("微信附件加密参数无效")
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            data = decryptor.update(data) + decryptor.finalize()
            padding = data[-1]
            if not 1 <= padding <= 16 or data[-padding:] != bytes([padding]) * padding:
                raise ValueError("微信附件解密校验失败")
            data = data[:-padding]
        return save_media_bytes(
            data,
            channel=self.name,
            file_name=str(detail.get("file_name") or ""),
            fallback_name=f"image-{index}" if is_image else f"attachment-{index}",
            image=is_image,
        )

    # ================= 状态持久化 =================

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._sync_buf = str(data.get("sync_buf") or "")
            self._context_tokens = dict(data.get("context_tokens") or {})
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("wechat 状态文件损坏，从零开始", exc_info=True)

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"sync_buf": self._sync_buf, "context_tokens": self._context_tokens},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)  # 原子替换，防写一半
        except Exception:
            logger.warning("wechat 状态保存失败", exc_info=True)

    # ================= 出站 =================

    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:
        """sendmessage：带回 context_token，超长按 2000 字分片。"""
        chat_id = reply.source.chat_id
        context_token = self._context_tokens.get(chat_id, "")
        for i in range(0, max(len(reply.text), 1), MAX_TEXT_CHUNK):
            chunk = reply.text[i : i + MAX_TEXT_CHUNK]
            msg: dict[str, Any] = {
                "from_user_id": "",
                "to_user_id": chat_id,
                "client_id": uuid.uuid4().hex,
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": chunk}}],
            }
            if context_token:
                msg["context_token"] = context_token
            result = self._post(EP_SEND_MESSAGE, {"msg": msg})
            if result.get("ret", 0) != 0 or result.get("errcode", 0) != 0:
                raise RuntimeError(f"wechat 出站失败 ret={result.get('ret')} errcode={result.get('errcode')}")
