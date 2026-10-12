"""Perception 的内部分级模型与唯一对外结果。"""
from __future__ import annotations
from enum import IntEnum


class Priority(IntEnum):
    """固定优先级；P2 内部再区分 Anchor 与近期原文。"""

    P0 = 4
    P1 = 3
    P2 = 2
    P3 = 1
