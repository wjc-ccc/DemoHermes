"""
Channel 注册表与自动启动 — Hermes platform_registry 的精简移植

核心规则（对齐 Hermes config.py 的 env 自动启用）：
    - registry.json 该 channel 节 "enable": false → 永不启动
    - env 凭据填齐且未显式关闭 → 自动 register + start
    - "enable": true 但凭据缺失 → warn 跳过；未显式启用且凭据缺失 → 静默跳过
    - 依赖缺失 / 单 channel 启动异常 → 只跳过自己，不拖垮其他 channel

凭据分工：registry.json（入库）只放开关与非敏感配置；密钥只放 .env。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from ..provider import CHANNEL_CONFIG
from .baseChannel import BaseChannel

logger = logging.getLogger(__name__)


@dataclass
class ChannelEntry:
    """一个平台 channel 的注册条目（Hermes PlatformEntry 精简版）。"""

    name: str                               # registry.json 里的 key，如 "feishu"
    label: str                              # 展示名，如 "飞书"
    factory: Callable[[dict], BaseChannel]   # 已解析配置只注入一次
    required_keys: tuple[str, ...] = ()     # 必需凭据的配置字段名
    env_map: dict[str, str] = field(default_factory=dict)  # 配置字段名 → env 变量名
    optional_env_map: dict[str, str] = field(default_factory=dict)  # 非必需运行参数
    check_deps: Callable[[], bool] = lambda: True          # 第三方依赖是否可用
    install_hint: str = ""                  # check_deps 为 False 时的提示


def resolve_channel_credentials(entry: ChannelEntry) -> dict[str, str] | None:
    """密钥只从环境变量读取；缺任何一个必需项返回 None。"""
    resolved: dict[str, str] = {}
    for key in entry.required_keys:
        env_name = entry.env_map.get(key)
        value = os.getenv(env_name, "") if env_name else ""
        value = str(value).strip()
        if not value:
            return None
        resolved[key] = value
    for key, env_name in entry.optional_env_map.items():
        value = str(os.getenv(env_name, "") or "").strip()
        if value:
            resolved[key] = value
    return resolved


def load_channel_config() -> dict:
    """读 CHANNEL_CONFIG 指向的 JSON；读不到返回空 dict。"""
    if not CHANNEL_CONFIG:
        return {}
    try:
        with open(CHANNEL_CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("channel 配置读取失败 path=%s err=%s", CHANNEL_CONFIG, e)
        return {}


def autostart_channels(
    gateway,
    *,
    config: dict | None = None,
    entries: dict[str, ChannelEntry] | None = None,
) -> list[BaseChannel]:
    """按配置自动启用各 channel：凭据齐 → register_channel + start。

    返回成功启动的 channel 列表；任何单点失败只记日志不抛出。
    """
    if config is None:
        config = load_channel_config()
    if entries is None:
        entries = CHANNEL_ENTRIES

    started: list[BaseChannel] = []
    for name, entry in entries.items():
        section = config.get(name) or {}
        if section.get("enable") is False:
            logger.info("channel=%s 配置 enable:false，跳过", name)
            continue

        credentials = resolve_channel_credentials(entry)
        if credentials is None:
            if section.get("enable") is True:
                logger.warning(
                    "channel=%s 已 enable 但凭据缺失（需要 %s，可走 env %s），跳过",
                    name, list(entry.required_keys), list(entry.env_map.values()),
                )
            else:
                logger.debug("channel=%s 凭据未配置，跳过", name)
            continue

        if not entry.check_deps():
            logger.warning("channel=%s 依赖缺失（%s），跳过", name, entry.install_hint)
            continue

        channel = None
        registered = False
        try:
            channel = entry.factory({**section, **credentials})
            gateway.register_channel(channel)
            registered = True
            channel.start()
            if not channel.status["start"]:
                raise RuntimeError("start() 未启动渠道")
        except Exception:
            logger.exception("channel=%s 启动失败，跳过（不影响其他 channel）", name)
            if channel is not None:
                try:
                    channel.stop()
                except Exception:
                    logger.exception("channel=%s 启动失败后关闭失败", name)
            if registered:
                gateway.unregister_channel(name)
            continue
        started.append(channel)
        logger.info("channel=%s（%s）已自动启动", name, entry.label)
    return started


# ---- 内置 channel 条目（工厂懒加载，避免 import 时拖累离线测试）----

def _deps_importable(*modules: str) -> Callable[[], bool]:
    def _check() -> bool:
        for mod in modules:
            try:
                __import__(mod)
            except ImportError:
                return False
        return True
    return _check


CHANNEL_ENTRIES: dict[str, ChannelEntry] = {
    "feishu": ChannelEntry(
        name="feishu",
        label="飞书",
        factory=lambda cfg: __import__(
            "agent.channel.channel_feishu", fromlist=["FeishuChannel"]
        ).FeishuChannel(cfg),
        required_keys=("app_id", "app_secret"),
        env_map={"app_id": "FEISHU_APP_ID", "app_secret": "FEISHU_APP_SECRET"},
        check_deps=_deps_importable("lark_oapi"),
        install_hint="pip install lark-oapi",
    ),
    "qq": ChannelEntry(
        name="qq",
        label="QQ 机器人",
        factory=lambda cfg: __import__(
            "agent.channel.channel_qq", fromlist=["QQChannel"]
        ).QQChannel(cfg),
        required_keys=("app_id", "client_secret"),
        env_map={"app_id": "QQ_APP_ID", "client_secret": "QQ_CLIENT_SECRET"},
        check_deps=_deps_importable("websockets", "httpx"),
        install_hint="pip install websockets httpx",
    ),
    "wechat": ChannelEntry(
        name="wechat",
        label="微信（iLink）",
        factory=lambda cfg: __import__(
            "agent.channel.channel_wechat", fromlist=["WechatChannel"]
        ).WechatChannel(cfg),
        required_keys=("token", "account_id"),
        env_map={"token": "WEIXIN_TOKEN", "account_id": "WEIXIN_ACCOUNT_ID"},
        optional_env_map={"base_url": "WEIXIN_BASE_URL"},
        check_deps=_deps_importable("httpx", "cryptography"),
        install_hint="pip install httpx cryptography",
    ),
}
