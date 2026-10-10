"""按运行时 scope 检索卡片、对话原文、工具结果和已登记文档。

职责：提供关键词搜索、按引用分页读取，以及少量经验卡的自动召回。
边界：只读；当前没有语义/RAG 检索，卡片写入与失效由存储层管理。
关联：memory_search/read 工具调用这里；MemoryContextLoader 调用 recall。
每次读取重新校验 scope，不能依赖模型传来的引用自行授权。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from agent.types.memory.items import UserMemoryItem
from agent.types.memory.scope import MemoryScope
from agent.types.memory.layers import MemoryLayer

from agent.types.memory.retrieval import MemoryReadPage, MemorySearchHit


_WORDS = re.compile(r"[a-z0-9_./+#-]+|[\u3400-\u9fff]+", re.IGNORECASE)
_GENERIC = frozenset({
    "help", "work", "problem", "question", "thing", "user", "memory",
    "帮助", "工作", "问题", "事情", "用户", "记忆", "内容", "以前", "怎么", "如何",
})


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())


def _terms(query: str, *, specific: bool = False) -> tuple[str, ...]:
    """保留具体词和汉字二元组，使较长中文问题也能命中对象词。"""
    values: list[str] = []
    for token in _WORDS.findall(query.casefold())[:32]:
        if specific and token in _GENERIC:
            continue
        values.append(token)
        if len(token) > 2 and all("\u3400" <= char <= "\u9fff" for char in token):
            values.extend(token[index:index + 2] for index in range(len(token) - 1))
    return tuple(dict.fromkeys(value for value in values
                               if len(value) >= (2 if specific or value.isascii() else 1)))[:32]


def _contains(text: str, token: str) -> bool:
    haystack, needle = _normal(text), _normal(token)
    if not needle:
        return False
    if needle.isascii() and needle[0].isalnum() and needle[-1].isalnum():
        return re.search(r"(?<![a-z0-9_])" + re.escape(needle) +
                         r"(?![a-z0-9_])", haystack) is not None
    return needle in haystack


def _specific_keyword(value: str) -> bool:
    keyword = _normal(value)
    return keyword not in _GENERIC and len(keyword) >= (3 if keyword.isascii() else 2)


def _timestamp(value: datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0


def _excerpt(value: str, terms: tuple[str, ...], *, limit: int = 260) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    positions = [text.casefold().find(term) for term in terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions) - 60) if positions else 0
    end = min(len(text), start + limit)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _allowed_document_scopes(scope: MemoryScope) -> tuple[tuple[str, str], ...]:
    return (
        ("user", scope.storage_key),
        ("project", MemoryScope(organization_id=scope.organization_id,
                                tenant_id=scope.tenant_id,
                                project_id=scope.project_id).storage_key),
        ("policy", MemoryScope(organization_id=scope.organization_id).storage_key),
    )


def _evidence_refs(item: UserMemoryItem) -> tuple[str, ...]:
    refs: list[str] = []
    for evidence in item.evidence:
        prefix = {"message": "message", "tool_call": "tool", "document": "document",
                  "manual": "manual"}.get(
            evidence.source_type)
        if prefix:
            refs.append(f"{prefix}:{evidence.source_id}")
    return tuple(dict.fromkeys(refs))


