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


@dataclass(frozen=True)
class MemoryReadPage:
    reference: str
    source: str
    content: str
    offset: int
    next_offset: int | None
    total_chars: int
    context: tuple[dict, ...] = ()
