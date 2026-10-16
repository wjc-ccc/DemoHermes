"""WechatChannel — 腾讯 iLink Bot API 长轮询 + sendmessage（网络全程 mock，离线可测）。

协议要点（对齐 Hermes weixin.py）：
    入站：POST {base}/ilink/bot/getupdates（35s 长轮询，get_updates_buf 游标推进）
    出站：POST {base}/ilink/bot/sendmessage，必须带回对端最新 context_token
    鉴权：Authorization Bearer token + AuthorizationType: ilink_bot_token
"""
from __future__ import annotations

import pytest
import httpx
from unittest.mock import MagicMock

from agent.core.channel.channel_wechat import (
    EP_GET_UPDATES,
    EP_SEND_MESSAGE,
    WechatChannel,
)
from agent.types.channel import OutboundReply, SessionSource


class CollectGateway:
    def __init__(self) -> None:
        self.events: list = []

    def submit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setenv("WEIXIN_TOKEN", "tok_wx")
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "bot@im.bot")
    ch = WechatChannel(
        {"token": "tok_wx", "account_id": "bot@im.bot"},
        state_path=tmp_path / "wechat_state.json",  # 不碰真实 data/
    )
    ch._gateway = CollectGateway()
    return ch


def _dm_msg(msg_id="wx1", text="在吗", sender="user_a@im.wechat", **kw) -> dict:
    msg = {
        "message_id": msg_id,
        "from_user_id": sender,
        "msg_type": 1,
        "context_token": "ctx_aaa",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    msg.update(kw)
    return msg


# ---- 鉴权头 ----

def test_headers_carry_bearer_and_ilink_fields(channel):
    headers = channel._headers('{"a":1}')
    assert headers["Authorization"] == "Bearer tok_wx"
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert headers["iLink-App-Id"] == "bot"
    assert "X-WECHAT-UIN" in headers


# ---- 长轮询 ----

def test_poll_once_advances_cursor_and_dispatches(channel):
    captured = []

    def fake_post(endpoint, payload, timeout=40.0):
        captured.append((endpoint, payload))
        return {"ret": 0, "msgs": [_dm_msg()], "get_updates_buf": "buf_2"}

    channel._post = fake_post
    channel._poll_once()
    assert channel._sync_buf == "buf_2"
    assert captured[0][0] == EP_GET_UPDATES
    assert captured[0][1]["get_updates_buf"] == ""
    assert len(channel._gateway.events) == 1


def test_poll_once_timeout_returns_empty(channel):
    channel._post = lambda *a, **k: {"ret": 0, "msgs": [], "get_updates_buf": "buf_keep"}
    channel._sync_buf = "buf_keep"
    channel._poll_once()
    assert channel._gateway.events == []
    assert channel._sync_buf == "buf_keep"


def test_connect_timeout_is_not_treated_as_empty_poll(channel):
    channel._post = lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectTimeout("unreachable"))
    with pytest.raises(httpx.ConnectTimeout):
        channel._poll_once()


def test_session_expired_detected(channel):
    assert channel._is_session_expired({"ret": 0, "errcode": -14}) is True
    assert channel._is_session_expired({"ret": 0, "msgs": []}) is False


# ---- 入站归一化 ----

def test_dm_message_becomes_inbound(channel):
    channel._ingest_message(_dm_msg())
    event = channel._gateway.events[0]
    assert event.text == "在吗"
    assert event.source.channel == "wechat"
    assert event.source.chat_type == "dm"
    assert event.source.chat_id == "user_a@im.wechat"
    assert event.source.user_id == "user_a@im.wechat"
    assert event.source.reply_to_message_id == "wx1"
    assert event.source.metadata == {}  # context_token 不随事件外流，只留在 channel 内
    assert channel._context_tokens["user_a@im.wechat"] == "ctx_aaa"


def test_group_message_via_room_id(channel):
    channel._ingest_message(_dm_msg(msg_id="wx2", room_id="room1@im.wechat"))
    event = channel._gateway.events[0]
    assert event.source.chat_type == "group"
    assert event.source.chat_id == "room1@im.wechat"


def test_duplicate_message_dropped(channel):
    channel._ingest_message(_dm_msg(msg_id="wx_dup"))
    channel._ingest_message(_dm_msg(msg_id="wx_dup"))
    assert len(channel._gateway.events) == 1


def test_empty_text_dropped(channel):
    channel._ingest_message(_dm_msg(msg_id="wx3", item_list=[{"type": 3, "image_item": {}}]))
    assert channel._gateway.events == []


def test_media_message_token_saved_and_deduped_on_poll_thread(channel, tmp_path):
    queued = []
    channel.submit_inbound_work = lambda fn, *args: queued.append(args)  # 线程池不执行
    media_msg = _dm_msg(msg_id="wx_img", context_token="ctx_img", item_list=[
        {"type": 2, "image_item": {"media": {"full_url": "https://a.weixin.qq.com/1"}}},
    ])
    channel._post = lambda *a, **k: {"ret": 0, "msgs": [media_msg, media_msg], "get_updates_buf": "b"}
    channel._poll_once()
    assert len(queued) == 1  # 重复消息在入队前就被去重
    fresh = WechatChannel(state_path=tmp_path / "wechat_state.json")
    assert fresh._context_tokens["user_a@im.wechat"] == "ctx_img"  # 工作线程未运行也已落盘


# ---- 状态持久化 ----

def test_state_roundtrip(channel, tmp_path):
    channel._sync_buf = "buf_9"
    channel._context_tokens["peer1"] = "ctx_9"
    channel._save_state()

    fresh = WechatChannel(state_path=tmp_path / "wechat_state.json")
    assert fresh._sync_buf == "buf_9"
    assert fresh._context_tokens["peer1"] == "ctx_9"


# ---- 出站投递 ----

def test_deliver_with_context_token(channel):
    sent = []
    channel._context_tokens["user_a@im.wechat"] = "ctx_aaa"
    channel._post = lambda endpoint, payload, timeout=15.0: sent.append((endpoint, payload)) or {}
    channel.deliver_to_OutboundEvent(OutboundReply(
        text="你好",
        source=SessionSource(channel="wechat", chat_id="user_a@im.wechat"),
    ))
    endpoint, payload = sent[0]
    assert endpoint == EP_SEND_MESSAGE
    msg = payload["msg"]
    assert msg["to_user_id"] == "user_a@im.wechat"
    assert msg["context_token"] == "ctx_aaa"
    assert msg["item_list"] == [{"type": 1, "text_item": {"text": "你好"}}]
    assert channel.status["outbound_event_count"] == 0  # Gateway 统一记账


def test_deliver_uses_latest_context_token_of_chat(channel):
    sent = []
    channel._ingest_message(_dm_msg(msg_id="wx_old", context_token="ctx_old"))
    channel._ingest_message(_dm_msg(msg_id="wx_new", context_token="ctx_new"))
    channel._post = lambda endpoint, payload, timeout=15.0: sent.append(payload) or {}
    # 回复第一条消息时，也要带对端最新的 token
    channel.deliver_to_OutboundEvent(OutboundReply(
        text="回复旧消息", source=channel._gateway.events[0].source,
    ))
    assert sent[0]["msg"]["context_token"] == "ctx_new"


def test_deliver_chunks_long_text(channel):
    sent = []
    channel._context_tokens["u"] = "ctx"
    channel._post = lambda endpoint, payload, timeout=15.0: sent.append(payload) or {}
    channel.deliver_to_OutboundEvent(OutboundReply(
        text="长" * 4500,
        source=SessionSource(channel="wechat", chat_id="u"),
    ))
    assert len(sent) == 3  # 2000 + 2000 + 500
    assert sent[0]["msg"]["item_list"][0]["text_item"]["text"] == "长" * 2000


def test_deliver_failure_counts_error(channel):
    def boom(*a, **k):
        raise RuntimeError("network down")
    channel._post = boom
    with pytest.raises(RuntimeError, match="network down"):
        channel.deliver_to_OutboundEvent(OutboundReply(
            text="x", source=SessionSource(channel="wechat", chat_id="u"),
        ))


def test_stop_closes_http_client(channel):
    channel._http = MagicMock()
    http = channel._http
    channel.status["connected"] = True
    channel.stop()
    http.close.assert_called_once()
    assert channel.status["connected"] is False
