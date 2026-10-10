"""QQChannel — 官方 bot 网关 wss + REST 投递（网络全程 mock，离线可测）。

协议要点（对齐 Hermes qqbot/adapter.py）：
    token：POST https://bots.qq.com/app/getAppAccessToken {appId, clientSecret}
    网关：GET  https://api.sgroup.qq.com/gateway → wss url
    wss ：op10 Hello → op2 Identify（intents/shard）→ READY（session_id）
          op1 心跳（带最新 seq）；op7/9 → 重连；op0 Dispatch → 消息事件
    出站：POST /v2/users/{openid}/messages（c2c）或 /v2/groups/{group_openid}/messages
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agent.channel.channel_qq import (
    GATEWAY_URL,
    TOKEN_URL,
    QQChannel,
)
from agent.types.channel import OutboundReply, SessionSource


class CollectGateway:
    def __init__(self) -> None:
        self.events: list = []

    def submit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def channel(monkeypatch):
    monkeypatch.setenv("QQ_APP_ID", "102000001")
    monkeypatch.setenv("QQ_CLIENT_SECRET", "secret_x")
    ch = QQChannel({"app_id": "102000001", "client_secret": "secret_x"})
    ch._gateway = CollectGateway()
    return ch


def _c2c_payload(msg_id="m1", content="你好", openid="U_OPENID_1") -> dict:
    return {
        "op": 0, "s": 11, "t": "C2C_MESSAGE_CREATE",
        "d": {"id": msg_id, "content": content,
              "author": {"user_openid": openid}},
    }


def _group_payload(msg_id="g1", content="  报个价", gid="G_OPENID_1", mid="M_OPENID_1") -> dict:
    return {
        "op": 0, "s": 12, "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {"id": msg_id, "content": content, "group_openid": gid,
              "author": {"member_openid": mid}},
    }


# ---- token 管理 ----

def test_token_fetched_and_cached(channel):
    calls = []

    def fake_post(url, payload, headers=None):
        calls.append(url)
        return {"access_token": "tok_1", "expires_in": "7200"}

    channel._post = fake_post
    assert channel._ensure_token() == "tok_1"
    assert channel._ensure_token() == "tok_1"  # 命中缓存
    assert calls == [TOKEN_URL]


def test_token_refreshes_near_expiry(channel):
    channel._post = lambda url, payload, headers=None: {"access_token": "tok_2", "expires_in": "7200"}
    channel._token = "tok_old"
    channel._token_expire_at = 0  # 已过期
    assert channel._ensure_token() == "tok_2"


# ---- wss 协议状态机 ----

def test_hello_sets_heartbeat_interval(channel):
    channel._handle_payload({"op": 10, "d": {"heartbeat_interval": 41250}})
    assert channel._heartbeat_interval == pytest.approx(41.25)


def test_identify_payload_shape(channel):
    channel._token = "tok_x"
    channel._token_expire_at = 1e18
    identify = channel._build_identify()
    assert identify["op"] == 2
    assert identify["d"]["token"] == "QQBot tok_x"
    assert identify["d"]["shard"] == [0, 1]
    assert identify["d"]["intents"] == 1 << 25  # 只订阅群聊 @ 与单聊


def test_ready_stores_session(channel):
    channel._handle_payload({"op": 0, "s": 1, "t": "READY",
                             "d": {"session_id": "sess_1"}})
    assert channel._session_id == "sess_1"
    assert channel._last_seq == 1
    assert channel.status["connected"] is True


def test_resume_when_session_exists(channel):
    channel._token = "tok_x"
    channel._token_expire_at = 1e18
    channel._session_id = "sess_1"
    channel._last_seq = 42
    resume = channel._build_resume()
    assert resume == {"op": 6, "d": {"token": "QQBot tok_x", "session_id": "sess_1", "seq": 42}}


def test_reconnect_ops_signal_reconnect(channel):
    channel.status["connected"] = True
    assert channel._handle_payload({"op": 7, "d": {}}) == "reconnect"
    assert channel.status["connected"] is False
    assert channel._handle_payload({"op": 9, "d": {"resumable": False}}) == "reconnect"


def test_heartbeat_uses_last_seq(channel):
    channel._last_seq = 77
    assert channel._build_heartbeat() == {"op": 1, "d": 77}


def test_backoff_ladder(channel):
    assert [channel._next_backoff(i) for i in range(7)] == [2, 5, 10, 30, 60, 60, 60]


# ---- 入站归一化 ----

def test_c2c_message_becomes_inbound(channel):
    channel._handle_payload(_c2c_payload())
    assert len(channel._gateway.events) == 1
    event = channel._gateway.events[0]
    assert event.text == "你好"
    assert event.source.channel == "qq"
    assert event.source.chat_type == "dm"
    assert event.source.chat_id == "U_OPENID_1"
    assert event.source.user_id == "U_OPENID_1"
    assert event.source.reply_to_message_id == "m1"


def test_group_message_strips_mention_and_marks_group(channel):
    channel._handle_payload(_group_payload(content=" <@!999> 报个价"))
    event = channel._gateway.events[0]
    assert event.text == "报个价"
    assert event.source.chat_type == "group"
    assert event.source.chat_id == "G_OPENID_1"
    assert event.source.user_id == "M_OPENID_1"


def test_duplicate_message_dropped(channel):
    channel._handle_payload(_c2c_payload(msg_id="m_dup"))
    channel._handle_payload(_c2c_payload(msg_id="m_dup"))
    assert len(channel._gateway.events) == 1
    assert channel.status["message_count"] == 2  # 平台推来两次
    assert channel.status["inbound_event_count"] == 1  # 去重后只送出一次


def test_seq_tracked_on_dispatch(channel):
    channel._handle_payload(_c2c_payload())
    assert channel._last_seq == 11


# ---- 出站投递 ----

def test_deliver_c2c(channel):
    sent = []
    channel._token = "tok_x"
    channel._token_expire_at = 1e18
    channel._post = lambda url, payload, headers=None: sent.append((url, payload, headers)) or {}
    channel.deliver_to_OutboundEvent(OutboundReply(
        text="回复你",
        source=SessionSource(channel="qq", chat_id="U_OPENID_1", chat_type="dm",
                             reply_to_message_id="m1"),
    ))
    url, payload, headers = sent[0]
    assert url.endswith("/v2/users/U_OPENID_1/messages")
    assert payload["content"] == "回复你"
    assert payload["msg_type"] == 0
    assert headers["Authorization"] == "QQBot tok_x"
    assert channel.status["outbound_event_count"] == 0  # Gateway 统一记账


def test_deliver_group(channel):
    sent = []
    channel._token = "tok_x"
    channel._token_expire_at = 1e18
    channel._post = lambda url, payload, headers=None: sent.append((url, payload, headers)) or {}
    channel.deliver_to_OutboundEvent(OutboundReply(
        text="群回复",
        source=SessionSource(channel="qq", chat_id="G_OPENID_1", chat_type="group"),
    ))
    assert sent[0][0].endswith("/v2/groups/G_OPENID_1/messages")


def test_deliver_failure_counts_error(channel):
    def boom(url, payload, headers=None):
        raise RuntimeError("network down")
    channel._post = boom
    with pytest.raises(RuntimeError, match="network down"):
        channel.deliver_to_OutboundEvent(OutboundReply(
            text="x", source=SessionSource(channel="qq", chat_id="U", chat_type="dm"),
        ))


def test_deliver_rejects_api_error_code(channel):
    channel._token = "tok_x"
    channel._token_expire_at = 1e18
    channel._post = lambda *a, **k: {"code": 40001}
    with pytest.raises(RuntimeError, match="code=40001"):
        channel.deliver_to_OutboundEvent(OutboundReply(
            text="x", source=SessionSource(channel="qq", chat_id="U", chat_type="dm"),
        ))


def test_stop_closes_websocket_and_http_client(channel):
    channel._ws = MagicMock()
    channel._http = MagicMock()
    ws, http = channel._ws, channel._http
    channel.status["connected"] = True
    channel.stop()
    ws.close.assert_called_once()
    http.close.assert_called_once()
    assert channel.status["connected"] is False
