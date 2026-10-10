"""
Channel 注册表与自动启动 — Hermes platform_registry 的精简移植

核心规则（对齐 Hermes config.py 的 env 自动启用）：
    - registry.json 该 channel 节 "enable": false → 永不启动
    - env 凭据填齐且未显式关闭 → 自动 register + start
    - "enable": true 但凭据缺失 → warn 跳过；未显式启用且凭据缺失 → 静默跳过
    - 依赖缺失 / 单 channel 启动异常 → 只跳过自己，不拖垮其他 channel

凭据分工：registry.json（入库）只放开关与非敏感配置；密钥只放 .env。
每个平台的名字、env 变量名、依赖都声明在 channel 类属性上，这里只读取。
"""
from __future__ import annotations

import importlib
import json
import logging
import os

from ..provider import CHANNEL_CONFIG
from .baseChannel import BaseChannel
from .channel_feishu import FeishuChannel
from .channel_qq import QQChannel
from .channel_wechat import WechatChannel

logger = logging.getLogger(__name__)

PLATFORM_CHANNELS: tuple[type[BaseChannel], ...] = (FeishuChannel, QQChannel, WechatChannel)


def resolve_channel_credentials(channel_cls: type[BaseChannel]) -> dict[str, str] | None:
    """密钥只从环境变量读取；缺任何一个必需项返回 None。"""
    resolved: dict[str, str] = {}
    for key, env_name in channel_cls.env_map.items():
        value = str(os.getenv(env_name, "") or "").strip()
        if not value:
            return None
        resolved[key] = value
    for key, env_name in channel_cls.optional_env_map.items():
        value = str(os.getenv(env_name, "") or "").strip()
        if value:
            resolved[key] = value
    return resolved


def deps_available(channel_cls: type[BaseChannel]) -> bool:
    for module in channel_cls.deps:
        try:
            importlib.import_module(module)
        except ImportError:
            return False
    return True


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
    channels: tuple[type[BaseChannel], ...] | None = None,
) -> list[BaseChannel]:
    """按配置自动启用各 channel：凭据齐 → register_channel + start。

    返回成功启动的 channel 列表；任何单点失败只记日志不抛出。
    """
    if config is None:
        config = load_channel_config()
    if channels is None:
        channels = PLATFORM_CHANNELS

    started: list[BaseChannel] = []
    for channel_cls in channels:
        name = channel_cls.name
        section = config.get(name) or {}
        if section.get("enable") is False:
            logger.info("channel=%s 配置 enable:false，跳过", name)
            continue

        credentials = resolve_channel_credentials(channel_cls)
        if credentials is None:
            if section.get("enable") is True:
                logger.warning(
                    "channel=%s 已 enable 但凭据缺失（需要 env %s），跳过",
                    name, list(channel_cls.env_map.values()),
                )
            else:
                logger.debug("channel=%s 凭据未配置，跳过", name)
            continue

        if not deps_available(channel_cls):
            logger.warning("channel=%s 依赖缺失（%s），跳过", name, channel_cls.install_hint)
            continue

        channel = None
        registered = False
        try:
            channel = channel_cls({**section, **credentials})
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
        logger.info("channel=%s（%s）已自动启动", name, channel_cls.label)
    return started
