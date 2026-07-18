"""
WeChat Channel 骨架 — 演示「平台字段 → 统一 InDTO」只在 Channel 发生。

真正连微信 SDK 后，把 raw webhook/payload 丢进 parse_to_InboundEvent 即可；
Gateway / Loop 零改动。
"""
from __future__ import annotations

from typing import Any

from .baseChannel import BaseChannel
from ..core.types import SessionSource, InboundEvent, OutboundReply, ContentPart

