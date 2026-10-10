"""结构化用户卡片的类别、经验结果和生命周期状态。

职责：统一提取、校验、存储与检索对卡片枚举值的理解。
边界：kind 是用户卡的唯一分类；原始轮次和手册不属于卡片。
关联：MemoryProposal、UserMemoryItem 和 SQLite memory_cards 共同使用这些值。
"""
from __future__ import annotations
from enum import Enum


### 用户卡片的类别
class UserCardKind(str, Enum):
    PROFILE = "profile"
    STABLE_PREFERENCE = "stable_preference"
    ONGOING_FACT = "ongoing_fact"
    REUSABLE_EXPERIENCE = "reusable_experience"


### 经验卡的成败
class ExperienceOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    MIXED = "mixed"
    OBSERVED = "observed"


### 用户卡的生命周期状态
class UserCardStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"
    EXPIRED = "expired"
    CONFLICTED = "conflicted"
    QUARANTINED = "quarantined"
