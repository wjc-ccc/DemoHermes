"""
Channel 注册表与自动启动 — Hermes platform_registry 的精简移植

核心规则（对齐 Hermes config.py 的 env 自动启用）：
    - registry.json 该 channel 节 "enable": false → 永不启动
    - 凭据填齐（JSON 同名 key 或 env 变量）且未显式关闭 → 自动 register + start
    - "enable": true 但凭据缺失 → warn 跳过；未显式启用且凭据缺失 → 静默跳过
    - 依赖缺失 / 单 channel 启动异常 → 只跳过自己，不拖垮其他 channel

凭据分工：registry.json（入库）只放开关与非敏感配置；密钥放 .env。
JSON 同名 key 也可直接写密钥（本地调试便利），但请勿提交。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from ..Provider import CHANNEL_CONFIG
from .baseChannel import BaseChannel

logger = logging.getLogger(__name__)


@dataclass
class ChannelEntry:
    """一个平台 channel 的注册条目（Hermes PlatformEntry 精简版）。"""

    name: str                               # registry.json 里的 key，如 "feishu"
    label: str                              # 展示名，如 "飞书"
    factory: Callable[[], BaseChannel]      # 无参工厂；channel 自行读配置
    required_keys: tuple[str, ...] = ()     # 必需凭据的 JSON key 名
    env_map: dict[str, str] = field(default_factory=dict)  # JSON key → env 变量名
    check_deps: Callable[[], bool] = lambda: True          # 第三方依赖是否可用
    install_hint: str = ""                  # check_deps 为 False 时的提示


def resolve_channel_credentials(entry: ChannelEntry, section: dict) -> dict[str, str] | None:
    """按「JSON 同名 key 优先，env 兜底」解析凭据；缺任何一个必需项返回 None。"""
    resolved: dict[str, str] = {}
    for key in entry.required_keys:
        value = section.get(key) or os.getenv(entry.env_map.get(key, ""), "")
        value = str(value).strip()
        if not value:
            return None
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

        credentials = resolve_channel_credentials(entry, section)
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

        try:
            channel = entry.factory()
            gateway.register_channel(channel)
            channel.start()
        except Exception:
            logger.exception("channel=%s 启动失败，跳过（不影响其他 channel）", name)
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
        factory=lambda: __import__(
            "Agent.channel.channel_feishu", fromlist=["FeishuChannel"]
        ).FeishuChannel(),
        required_keys=("app_id", "app_secret"),
        env_map={"app_id": "FEISHU_APP_ID", "app_secret": "FEISHU_APP_SECRET"},
        check_deps=_deps_importable("lark_oapi"),
        install_hint="pip install lark-oapi",
    ),
    "qq": ChannelEntry(
        name="qq",
        label="QQ 机器人",
        factory=lambda: __import__(
            "Agent.channel.channel_qq", fromlist=["QQChannel"]
        ).QQChannel(),
        required_keys=("app_id", "client_secret"),
        env_map={"app_id": "QQ_APP_ID", "client_secret": "QQ_CLIENT_SECRET"},
        check_deps=_deps_importable("websockets", "httpx"),
        install_hint="pip install websockets httpx",
    ),
    "wechat": ChannelEntry(
        name="wechat",
        label="微信（iLink）",
        factory=lambda: __import__(
            "Agent.channel.channel_wechat", fromlist=["WechatChannel"]
        ).WechatChannel(),
        required_keys=("token", "account_id"),
        env_map={"token": "WEIXIN_TOKEN", "account_id": "WEIXIN_ACCOUNT_ID"},
        check_deps=_deps_importable("httpx"),
        install_hint="pip install httpx",
    ),
}
