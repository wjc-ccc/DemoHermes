"""MemoryTrace：记录提取、校验、持久化、召回与加载决定。"""
from __future__ import annotations
from enum import Enum


# 职责：区分模型自报字段和运行时核验字段，记录每张候选卡的处理决定。
# 边界：只定义审计结构，不产生提案，也不负责写入 traces 表。
# 关联：Reviewer/Committer 生成与补全 Trace，SQLite trace_store.py 保存。


class MemoryTraceOperation(str, Enum):
    EXTRACT = "extract"
    VALIDATE = "validate"
    PERSIST = "persist"
    RETRIEVE = "retrieve"
    LOAD = "load"


class MemoryTraceDecision(str, Enum):
    ACCEPTED = "accepted"
    MERGED = "merged"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    SKIPPED = "skipped"
