"""memory / retrieval 数据契约。"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class MemorySearchHit:
    reference: str
    source: str  # 来源类别：卡片、对话历史或原文。
    title: str
    snippet: str
    matched_keywords: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    score: float
    created_at: float
    kind: str | None = None
    content: str | None = None  # 自动召回使用的有界正文。
    match_reason: str = ""
    item_id: str | None = None
    revision: int | None = None  # 卡片修订号，供 memory_edit 乐观锁使用。
    read_offset: int = 0  # 原文命中位置附近的字符偏移，可直接传给 memory_read。
    card_snapshot: dict | None = None  # 仅供冻结实际召回版本，不向搜索工具输出。


@dataclass(frozen=True)
class MemoryReadPage:
    reference: str
    source: str
    content: str
    offset: int
    next_offset: int | None
    total_chars: int
    context: tuple[dict, ...] = ()
    card_revision: int | None = None  # 读取时绑定的版本，只供实际曝光记账使用。
