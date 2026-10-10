"""FeishuChannel — lark ws 长连 + REST 投递（平台 SDK 全程 mock，离线可测）。

raw 约定（channel 内部把 lark event 提取成纯 dict 后交给 parse）：
    {
        "message_id": "om_xxx",
        "chat_id": "oc_xxx",
        "chat_type": "dm" | "group",   # event_to_raw 已把飞书的 p2p 归一为 dm
        "user_id": "ou_xxx",
        "message_type": "text",
        "text": "消息正文",
        "mentions": [{"open_id": "ou_bot", "name": "bot", "key": "@_user_1"}],
    }
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.channel.channel_feishu import FeishuChannel
from agent.types.channel import OutboundReply, SessionSource


def _raw(**over) -> dict:
    raw = {
        "message_id": "om_1",
        "chat_id": "oc_chat1",
        "chat_type": "dm",
        "user_id": "ou_user1",
        "message_type": "text",
        "text": "你好",
        "mentions": [],
    }
    raw.update(over)
    return raw


@pytest.fixture
def channel(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_xxx")
    return FeishuChannel({"app_id": "cli_xxx", "app_secret": "secret_xxx"})


# ---- 入站归一化 ----

def test_parse_dm_text(channel):
    event = channel.parse_to_InboundEvent(_raw())
    assert event.text == "你好"
    assert event.source.channel == "feishu"
    assert event.source.chat_id == "oc_chat1"
    assert event.source.chat_type == "dm"
    assert event.source.user_id == "ou_user1"
    assert event.source.reply_to_message_id == "om_1"  # 回程车票
    assert "飞书" in (event.channel_prompt or "")


def test_parse_group_is_group_type(channel):
    event = channel.parse_to_InboundEvent(_raw(chat_type="group"))
    assert event.source.chat_type == "group"


def test_parse_strips_mention_key_from_text(channel):
    event = channel.parse_to_InboundEvent(_raw(
        chat_type="group",
        text="@_user_1 报价单发我",
        mentions=[{"open_id": "ou_bot", "name": "bot", "key": "@_user_1"}],
    ))
    assert event.text == "报价单发我"


# ---- 准入：去重 + 群聊 @ 要求 ----

def test_should_accept_dedup_by_message_id(channel):
    raw = _raw()
    assert channel.should_accept(raw) is True
    assert channel.should_accept(raw) is False  # 同 message_id 重放


def test_should_accept_distinct_message_ids(channel):
    assert channel.should_accept(_raw(message_id="om_a")) is True
    assert channel.should_accept(_raw(message_id="om_b")) is True


def test_group_require_mention_filters_when_bot_known(channel):
    channel._bot_open_id = "ou_bot"
    not_for_bot = _raw(chat_type="group", mentions=[{"open_id": "ou_other", "key": "@_user_9"}])
    for_bot = _raw(chat_type="group", message_id="om_2",
                   mentions=[{"open_id": "ou_bot", "key": "@_user_1"}])
    assert channel.should_accept(not_for_bot) is False
    assert channel.should_accept(for_bot) is True


def test_group_without_require_mention_accepts_all(channel):
    channel.require_mention = False
    channel._bot_open_id = "ou_bot"
    assert channel.should_accept(_raw(chat_type="group", mentions=[])) is True


def test_dm_always_accepted(channel):
    channel._bot_open_id = "ou_bot"
    assert channel.should_accept(_raw(chat_type="dm", mentions=[])) is True


# ---- lark event 对象 → raw dict ----

def test_event_to_raw_extracts_fields(channel):
    mention = SimpleNamespace(key="@_user_1", name="bot",
                              id=SimpleNamespace(open_id="ou_bot"))
    message = SimpleNamespace(
        message_id="om_9", chat_id="oc_9", chat_type="group",
        message_type="text", content=json.dumps({"text": "@_user_1 在吗"}),
        mentions=[mention],
    )
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_u9"),
                             sender_type="user")
    data = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))
    raw = channel.event_to_raw(data)
    assert raw["message_id"] == "om_9"
    assert raw["chat_id"] == "oc_9"
    assert raw["chat_type"] == "group"
    assert raw["user_id"] == "ou_u9"
    assert raw["text"] == "@_user_1 在吗"
    assert raw["mentions"] == [{"open_id": "ou_bot", "name": "bot", "key": "@_user_1"}]


def test_event_to_raw_non_text_falls_back(channel):
    message = SimpleNamespace(
        message_id="om_img", chat_id="oc_9", chat_type="p2p",
        message_type="image", content=json.dumps({"image_key": "img_v2_x"}),
        mentions=None,
    )
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_u9"), sender_type="user")
    raw = channel.event_to_raw(SimpleNamespace(event=SimpleNamespace(message=message, sender=sender)))
    assert raw["resource"]["image_key"] == "img_v2_x"
    assert raw["chat_type"] == "dm"  # 飞书 p2p 在 raw 阶段归一


# ---- 出站投递（mock lark client）----

def _reply(**kw) -> OutboundReply:
    base = dict(
        text="你好，我是 bot",
        source=SessionSource(channel="feishu", chat_id="oc_chat1",
                             chat_type="dm", user_id="ou_user1",
                             reply_to_message_id="om_1"),
    )
    base.update(kw)
    return OutboundReply(**base)


def test_deliver_prefers_reply_to_message(channel):
    client = MagicMock()
    client.im.v1.message.reply.return_value = SimpleNamespace(success=lambda: True)
    channel._client = client
    channel.deliver_to_OutboundEvent(_reply())
    assert client.im.v1.message.reply.called
    req = client.im.v1.message.reply.call_args[0][0]
    assert req.message_id == "om_1"
    assert not client.im.v1.message.create.called
    assert channel.status["outbound_event_count"] == 0  # Gateway 投递成功后统一记账


def test_deliver_falls_back_to_create_when_no_reply_to(channel):
    client = MagicMock()
    client.im.v1.message.create.return_value = SimpleNamespace(success=lambda: True)
    channel._client = client
    channel.deliver_to_OutboundEvent(_reply(source=SessionSource(channel="feishu", chat_id="oc_chat1")))
    assert client.im.v1.message.create.called
    req = client.im.v1.message.create.call_args[0][0]
    # 统一用 chat_id 寻址（p2p / group 均可）
    assert req.request_body.receive_id == "oc_chat1"
    assert req.receive_id_type == "chat_id"


def test_deliver_reply_failure_falls_back_to_create(channel):
    client = MagicMock()
    client.im.v1.message.reply.return_value = SimpleNamespace(success=lambda: False, msg="withdrawn")
    client.im.v1.message.create.return_value = SimpleNamespace(success=lambda: True)
    channel._client = client
    channel.deliver_to_OutboundEvent(_reply())
    assert client.im.v1.message.reply.called and client.im.v1.message.create.called


def test_deliver_without_client_counts_error(channel):
    channel._client = None
    with pytest.raises(RuntimeError, match="REST client"):
        channel.deliver_to_OutboundEvent(_reply())
