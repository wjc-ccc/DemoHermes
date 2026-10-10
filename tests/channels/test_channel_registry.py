"""Channel 注册表 + 自动启动（Hermes 式：凭据填齐即自动连接）。

配置矩阵：
    enable:false            → 永不启动（即使凭据齐全）
    enable 缺省 + 凭据齐    → 自动注册并 start
    enable 缺省 + 凭据缺    → 静默跳过
    enable:true  + 凭据缺   → 跳过（warn，不炸）
    依赖缺失                → 跳过（install_hint）
    单个 channel 异常       → 不拖垮其他 channel
    JSON 节里的同名 key     → 不作为凭据
"""
from __future__ import annotations

import logging

import pytest

from agent.channel.baseChannel import BaseChannel
from agent.channel.registry import (
    PLATFORM_CHANNELS,
    autostart_channels,
    resolve_channel_credentials,
)
from agent.types.channel import InboundEvent, OutboundReply


class FakeChannel(BaseChannel):
    """不连任何平台的假 channel，只记录生命周期。"""

    name = "fake"
    label = "FAKE"
    env_map = {"api_key": "FAKE_API_KEY"}
    install_hint = "pip install fake"

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True
        self.status["start"] = True

    def stop(self) -> None:
        self.stopped = True
        self.status["start"] = False

    def parse_to_InboundEvent(self, raw):  # pragma: no cover - 不在本测试范围
        return InboundEvent(text=str(raw))

    def deliver_to_OutboundEvent(self, reply: OutboundReply) -> None:  # pragma: no cover
        pass


class BoomChannel(FakeChannel):
    name = "boom"

    def start(self) -> None:
        raise RuntimeError("connect failed")


class FakeGateway:
    def __init__(self) -> None:
        self.channels: dict[str, BaseChannel] = {}

    def register_channel(self, channel: BaseChannel) -> None:
        self.channels[channel.name] = channel
        channel.bind_gateway(self)

    def get_channel(self, name: str):
        return self.channels.get(name)

    def unregister_channel(self, name: str):
        channel = self.channels.pop(name, None)
        if channel is not None:
            channel.unbind_gateway()

    def submit(self, event) -> None:  # pragma: no cover - send_gateway 备用
        pass


class MissingDepsChannel(FakeChannel):
    deps = ("module_that_does_not_exist_for_tests",)


class OptionalEnvChannel(FakeChannel):
    optional_env_map = {"base_url": "WEIXIN_BASE_URL"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FAKE_API_KEY", raising=False)


# ---- 凭据解析 ----

def test_resolve_credentials_from_env(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "k1")
    assert resolve_channel_credentials(FakeChannel) == {"api_key": "k1"}


def test_optional_env_is_injected_with_credentials(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "k1")
    monkeypatch.setenv("WEIXIN_BASE_URL", "https://regional.weixin.qq.com")
    assert resolve_channel_credentials(OptionalEnvChannel) == {
        "api_key": "k1",
        "base_url": "https://regional.weixin.qq.com",
    }


def test_wechat_account_id_is_optional(monkeypatch):
    from agent.channel.channel_wechat import WechatChannel

    monkeypatch.setenv("WEIXIN_TOKEN", "tok")
    monkeypatch.delenv("WEIXIN_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("WEIXIN_BASE_URL", raising=False)
    assert resolve_channel_credentials(WechatChannel) == {"token": "tok"}


def test_resolve_credentials_missing_returns_none():
    assert resolve_channel_credentials(FakeChannel) is None


def test_start_rejects_missing_credentials_from_env_map():
    with pytest.raises(RuntimeError, match="api_key"):
        FakeChannel({})._require_credentials()


# ---- 自动启动矩阵 ----

def test_autostart_with_env_credentials(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "k1")
    gw = FakeGateway()
    started = autostart_channels(gw, config={"fake": {}}, channels=(FakeChannel,))
    assert [c.name for c in started] == ["fake"]
    ch = gw.get_channel("fake")
    assert ch is not None and ch.started
    assert ch.config["api_key"] == "k1"
    assert ch._gateway is gw  # bind_gateway 已回调


def test_autostart_skips_when_enable_false(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "k1")
    gw = FakeGateway()
    started = autostart_channels(gw, config={"fake": {"enable": False}}, channels=(FakeChannel,))
    assert started == []
    assert gw.get_channel("fake") is None


def test_autostart_skips_silently_without_credentials():
    gw = FakeGateway()
    started = autostart_channels(gw, config={"fake": {}}, channels=(FakeChannel,))
    assert started == []


def test_autostart_warns_when_enabled_but_missing_credentials(caplog):
    gw = FakeGateway()
    with caplog.at_level(logging.WARNING):
        started = autostart_channels(gw, config={"fake": {"enable": True}}, channels=(FakeChannel,))
    assert started == []
    assert any("fake" in r.message for r in caplog.records)


def test_autostart_skips_when_deps_missing(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "k1")
    gw = FakeGateway()
    started = autostart_channels(gw, config={"fake": {}}, channels=(MissingDepsChannel,))
    assert started == []


def test_autostart_rejects_json_credentials(monkeypatch):
    """密钥只从 env 读取，避免把 JSON 中的旧密钥误用作有效凭据。"""
    gw = FakeGateway()
    started = autostart_channels(
        gw, config={"fake": {"api_key": "json-key"}}, channels=(FakeChannel,)
    )
    assert started == []


def test_one_channel_failure_does_not_block_others(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "k1")
    gw = FakeGateway()
    started = autostart_channels(
        gw, config={"boom": {}, "fake": {}}, channels=(BoomChannel, FakeChannel),
    )
    assert [c.name for c in started] == ["fake"]
    # boom 启动失败后注销，不影响 fake
    assert gw.get_channel("fake") is not None
    assert gw.get_channel("boom") is None


def test_builtin_channels_cover_feishu_qq_wechat():
    assert {cls.name for cls in PLATFORM_CHANNELS} >= {"feishu", "qq", "wechat"}
    for cls in PLATFORM_CHANNELS:
        assert cls.label, cls.name
        assert cls.env_map, cls.name
        assert cls.deps, cls.name
