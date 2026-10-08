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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .markdown_book import estimate_markdown_tokens
from .memory_item import UserMemoryItem
from .memory_scope import MemoryScope


_WORDS = re.compile(r"[a-z0-9_./+#-]+|[\u3400-\u9fff]+", re.IGNORECASE)
_GENERIC = frozenset({
    "help", "work", "problem", "question", "thing", "user", "memory",
    "帮助", "工作", "问题", "事情", "用户", "记忆", "内容", "以前", "怎么", "如何",
})


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


class MemoryRetriever:
    """只读检索；每次操作都由调用方提供可信的 MemoryScope。"""

    def __init__(self, database, *, document_root: str | Path | None = None,
                 allowed_document_paths: Iterable[str | Path] = ()) -> None:
        self.db = database
        self.document_root = Path(document_root).resolve() if document_root else None
        self.allowed_document_paths = frozenset(Path(path).resolve() for path in allowed_document_paths)

    def recall(self, scope: MemoryScope, query: str, *, limit: int = 3,
               token_budget: int = 600) -> tuple[MemorySearchHit, ...]:
        self._require_scope(scope)
        if scope.scope_kind != "personal" or not query.strip() or limit <= 0 or token_budget <= 0:
            return ()
        terms = _terms(query[:512], specific=True)
        if not terms:
            return ()
        ranked: list[tuple[float, float, str, UserMemoryItem, tuple[str, ...]]] = []
        for item in self._cards(scope, kind="reusable_experience"):
            if not self._complete_experience(item):
                continue
            keywords = tuple(keyword for keyword in item.keywords
                             if _specific_keyword(keyword) and _contains(query, keyword))
            if not keywords:
                continue
            applicability = getattr(item, "applicability", None) or ""
            condition_hits = sum(_contains(applicability, term) for term in terms)
            score = 10 * len(keywords) + 2 * condition_hits + item.priority / 100
            ranked.append((score, _timestamp(item.updated_at), item.item_id, item, keywords))
        ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
        remaining = token_budget
        hits: list[MemorySearchHit] = []
        for score, updated_at, _, item, keywords in ranked:
            if len(hits) >= min(limit, 3):
                break
            content = self._experience_text(item)
            cost = estimate_markdown_tokens(content)
            if cost > remaining:
                continue
            remaining -= cost
            hits.append(MemorySearchHit(
                reference=f"card:{item.item_id}", source="card",
                title=item.title or item.summary or item.slot_key or item.item_id,
                snippet=_excerpt(item.summary or item.content, terms),
                matched_keywords=keywords, evidence_refs=_evidence_refs(item),
                score=score, created_at=updated_at,
                kind="reusable_experience", content=content,
                match_reason="命中经验关键词：" + "、".join(keywords),
            ))
        return tuple(hits)

    def search(self, scope: MemoryScope, query: str, *, source: str = "all",
               kind: str | None = None, limit: int = 10,
               offset: int = 0) -> tuple[MemorySearchHit, ...]:
        self._require_scope(scope)
        if source not in {"all", "card", "history", "raw"}:
            raise ValueError("source 必须为 all/card/history/raw")
        if kind not in {None, "profile", "stable_preference", "ongoing_fact", "reusable_experience"}:
            raise ValueError("未知记忆卡片类别")
        query = query.strip()[:512]
        terms = _terms(query)
        if not terms:
            return ()
        results: list[MemorySearchHit] = []
        if source in {"all", "card"}:
            results.extend(self._search_cards(scope, query, terms, kind))
        if source in {"all", "history"}:
            results.extend(self._search_messages(scope, terms))
        if source in {"all", "raw"}:
            results.extend(self._search_tools(scope, terms))
            results.extend(self._search_documents(scope, terms))
        results.sort(key=lambda hit: (-hit.score, -hit.created_at, hit.reference))
        return tuple(results[max(offset, 0):max(offset, 0) + max(1, min(limit, 50))])

    def read(self, scope: MemoryScope, reference: str, *, offset: int = 0,
             limit: int = 2000) -> MemoryReadPage | None:
        self._require_scope(scope)
        prefix, separator, identity = reference.partition(":")
        if not separator or not identity or len(identity) > 256:
            return None
        context: tuple[dict, ...] = ()
        if prefix == "card":
            from ...storage.sqlite.memory_store import read_user_memory
            with self.db.read() as conn:
                item = read_user_memory(conn, identity, scope_key=scope.storage_key)
            if (item is None or item.status.value != "active" or
                (item.valid_from and item.valid_from.timestamp() * 1000 > self._now_ms()) or
                (item.valid_until and item.valid_until.timestamp() * 1000 <= self._now_ms())):
                return None
            body = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, indent=2)
            source = "card"
        elif prefix == "message":
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT m.session_id,m.seq_id,m.role,m.content_text,m.source_type "
                    "FROM messages m JOIN sessions s ON s.session_id=m.session_id "
                    "WHERE m.message_id=? AND s.scope_key=? AND m.role IN ('user','assistant')",
                    (identity, scope.storage_key),
                ).fetchone()
                if not row:
                    return None
                neighbors = conn.execute(
                    "SELECT message_id,role,content_text FROM messages WHERE session_id=? "
                    "AND seq_id BETWEEN ? AND ? AND role IN ('user','assistant') "
                    "ORDER BY seq_id", (row[0], row[1] - 1, row[1] + 1),
                ).fetchall()
            body = row[3]
            context = tuple({"reference": f"message:{mid}", "role": role,
                             "snippet": _excerpt(text, (), limit=200)}
                            for mid, role, text in neighbors if mid != identity)
            source = "history"
        elif prefix == "tool":
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT c.payload_json FROM tool_calls c JOIN sessions s "
                    "ON s.session_id=c.session_id WHERE c.tool_call_id=? AND s.scope_key=?",
                    (identity, scope.storage_key),
                ).fetchone()
            if not row:
                return None
            body = self._tool_content(json.loads(row[0]))
            source = "raw"
        elif prefix == "document":
            row = self._document(scope, identity)
            if not row:
                return None
            body = self._registered_content(scope, row[1], row[2], row[3])
            if body is None:
                return None
            actual_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if actual_hash != row[3]:
                context = ({"notice": "文档内容已改变；返回当前文件内容，注册时版本已过期"},)
            source = "raw"
        elif prefix == "manual":
            try:
                revision = int(identity)
            except ValueError:
                return None
            if revision < 1 or scope.scope_kind != "personal":
                return None
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT content FROM memory_document_versions WHERE scope_key=? AND revision=?",
                    (scope.storage_key, revision),
                ).fetchone()
            if row is None:
                return None
            body = row[0]
            source = "raw"
        else:
            return None
        offset, limit = max(0, offset), max(1, min(limit, 4000))
        end = min(len(body), offset + limit)
        return MemoryReadPage(reference=reference, source=source, content=body[offset:end],
                              offset=offset, next_offset=end if end < len(body) else None,
                              total_chars=len(body), context=context)

    @staticmethod
    def _require_scope(scope: MemoryScope) -> None:
        if not isinstance(scope, MemoryScope):
            raise ValueError("缺少运行时 memory scope")

    @staticmethod
    def _now_ms() -> int:
        from ...storage.sqlite.codec import milliseconds
        return milliseconds()

    def _cards(self, scope: MemoryScope, *, kind: str | None = None):
        from ...storage.sqlite.memory_store import query_user_memories
        where = "scope_key=? AND status='active' AND (valid_from IS NULL OR valid_from<=?) "
        where += "AND (valid_until IS NULL OR valid_until>?)"
        now = self._now_ms()
        args: list = [scope.storage_key, now, now]
        if kind:
            where += " AND kind=?"
            args.append(kind)
        with self.db.read() as conn:
            yield from query_user_memories(conn, where, tuple(args))

    @staticmethod
    def _experience_text(item: UserMemoryItem) -> str:
        title = item.title or item.summary or item.slot_key or item.item_id
        parts = [f"经验卡片 {title}（card:{item.item_id}；来源：{item.source_authority}）"]
        if item.outcome_status is not None:
            label = {"success": "成功观察", "failure": "失败观察",
                     "mixed": "结果不一", "observed": "单次观察"}[item.outcome_status.value]
            parts.append(f"结果类型：{label}")
        for label, field in (("适用条件", "applicability"), ("做法", "action"),
                             ("观察结果", "outcome"), ("限制", "limitations")):
            value = getattr(item, field, None)
            if value:
                parts.append(f"{label}：{value}")
        if len(parts) == 1:
            parts.append(item.content)
        refs = _evidence_refs(item)[:3]
        if refs:
            parts.append("证据：" + "、".join(refs))
        return "\n".join(parts)

    @staticmethod
    def _complete_experience(item: UserMemoryItem) -> bool:
        # 旧卡可能缺少结构化经验字段；可搜索，但重新校验前不能自动当成建议召回。
        fields = (item.applicability, item.action, item.outcome, item.limitations)
        keywords = {keyword.casefold().strip() for keyword in item.keywords
                    if _specific_keyword(keyword)}
        return (item.outcome_status is not None
                and all(value and value.strip() for value in fields)
                and 3 <= len(keywords) <= 8)

    def _search_cards(self, scope, query, terms, kind):
        hits = []
        for item in self._cards(scope, kind=kind):
            keywords = tuple(keyword for keyword in item.keywords if _specific_keyword(keyword)
                             and any(_contains(keyword, term) or _contains(query, keyword) for term in terms))
            summary = item.summary or ""
            applicability = getattr(item, "applicability", None) or ""
            title_hits = sum(_contains(item.title or "", term) for term in terms)
            summary_hits = sum(_contains(summary, term) for term in terms)
            applicability_hits = sum(_contains(applicability, term) for term in terms)
            content_hits = sum(_contains(item.content, term) for term in terms)
            score = 6 * len(keywords) + 2 * title_hits + 2 * applicability_hits + summary_hits + content_hits
            if score <= 0:
                continue
            hits.append(MemorySearchHit(
                reference=f"card:{item.item_id}", source="card",
                title=item.title or summary or item.slot_key or item.item_id,
                snippet=_excerpt(summary or item.content, terms), matched_keywords=keywords,
                evidence_refs=_evidence_refs(item),
                score=float(score) + item.priority / 100, created_at=_timestamp(item.updated_at),
                kind=item.kind.value,
                match_reason="关键词命中" if keywords else "正文命中",
                item_id=item.item_id, revision=item.revision,
            ))
        return hits

    def _search_messages(self, scope, terms):
        where = " OR ".join("instr(py_casefold(m.content_text),?)>0" for _ in terms)
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT m.message_id,m.role,m.content_text,m.created_at FROM messages m "
                "JOIN sessions s ON s.session_id=m.session_id WHERE s.scope_key=? "
                "AND m.role IN ('user','assistant') AND (" + where + ")",
                (scope.storage_key, *terms),
            ).fetchall()
        hits = []
        for identity, role, content, created_at in rows:
            matches = tuple(term for term in terms if _contains(content, term))
            if not matches:
                continue
            hits.append(MemorySearchHit(
                reference=f"message:{identity}", source="history",
                title="用户消息" if role == "user" else "助手回复",
                snippet=_excerpt(content, matches), matched_keywords=matches, evidence_refs=(),
                score=float(len(matches) * 2), created_at=created_at / 1000,
                match_reason="对话原文命中",
            ))
        return hits

    def _search_tools(self, scope, terms):
        where = " OR ".join("instr(py_casefold(c.payload_json),?)>0" for _ in terms)
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT c.tool_call_id,c.tool_name,c.payload_json,c.created_at FROM tool_calls c "
                "JOIN sessions s ON s.session_id=c.session_id WHERE s.scope_key=? "
                "AND c.tool_name NOT LIKE 'memory\\_%' ESCAPE '\\' AND (" + where + ")",
                (scope.storage_key, *terms),
            ).fetchall()
        hits = []
        for identity, name, payload, created_at in rows:
            content = self._tool_content(json.loads(payload))
            matches = tuple(term for term in terms if _contains(content, term))
            if not matches:
                continue
            hits.append(MemorySearchHit(
                reference=f"tool:{identity}", source="raw", title=f"工具输出：{name}",
                snippet=_excerpt(content, matches), matched_keywords=matches, evidence_refs=(),
                score=float(len(matches) * 2), created_at=created_at / 1000,
                match_reason="工具原文命中",
            ))
        return hits

    @staticmethod
    def _tool_content(payload: dict) -> str:
        result = payload.get("result_text")
        if result:
            return str(result)
        structured = payload.get("tool_call_result")
        return json.dumps(structured, ensure_ascii=False) if structured is not None else ""

    def _document(self, scope: MemoryScope, identity: str):
        clauses = ["(layer=? AND scope_key=?)" for _ in _allowed_document_scopes(scope)]
        values = [value for pair in _allowed_document_scopes(scope) for value in pair]
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT id,layer,path,content_hash,updated_at FROM context_documents WHERE id=? AND (" +
                " OR ".join(clauses) + ")", (identity, *values),
            ).fetchone()
        return row if row and self._document_path_allowed(scope, row[1], row[2]) else None

    def _document_path_allowed(self, scope: MemoryScope, layer: str, path: str) -> bool:
        resolved = Path(path).resolve()
        if layer == "user":
            return self.document_root is not None and resolved.is_relative_to(
                self.document_root / scope.storage_key)
        return resolved in self.allowed_document_paths

    @staticmethod
    def _read_registered_document(path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def _registered_content(self, scope: MemoryScope, layer: str, path: str,
                            registered_hash: str) -> str | None:
        current = self._read_registered_document(path)
        if layer != "user" or Path(path).name != "MEMORY.md":
            return current
        if current is not None and hashlib.sha256(current.encode("utf-8")).hexdigest() == registered_hash:
            return current
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT content FROM memory_document_versions WHERE scope_key=? "
                "ORDER BY revision DESC LIMIT 1", (scope.storage_key,),
            ).fetchone()
        return row[0] if row is not None else None

    def _search_documents(self, scope, terms):
        clauses = ["(layer=? AND scope_key=?)" for _ in _allowed_document_scopes(scope)]
        values = [value for pair in _allowed_document_scopes(scope) for value in pair]
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT id,layer,path,content_hash,updated_at FROM context_documents WHERE " + " OR ".join(clauses),
                tuple(values),
            ).fetchall()
        hits = []
        for identity, layer, path, content_hash, updated_at in rows:
            if not self._document_path_allowed(scope, layer, path):
                continue
            content = self._registered_content(scope, layer, path, content_hash)
            if content is None:
                continue
            matches = tuple(term for term in terms if _contains(content, term))
            if not matches:
                continue
            hits.append(MemorySearchHit(
                reference=f"document:{identity}", source="raw", title=Path(path).name,
                snippet=_excerpt(content, matches), matched_keywords=matches, evidence_refs=(),
                score=float(len(matches)), created_at=updated_at / 1000,
                match_reason="已登记 Markdown 命中",
            ))
        return hits
