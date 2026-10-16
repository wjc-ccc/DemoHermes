"""SQLite 记忆检索实现：原检索排序、作用域与分页规则保持。"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Iterable
from agent.types.memory.items import UserMemoryItem
from agent.types.memory.scope import MemoryScope
from agent.types.memory.layers import MemoryLayer
from agent.types.memory.retrieval import MemoryReadPage, MemorySearchHit
from agent.types.memory.document import MemoryManualError
from agent.core.memory.document_text import estimate_markdown_tokens
from agent.core.memory.experience_rendering import experience_entry, experience_block
from agent.core.memory.config import MemoryExperienceConfig
from agent.core.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.core.memory.retrieval_terms import _normal, _terms, _contains, _specific_keyword, _timestamp, _excerpt, _allowed_document_scopes, _evidence_refs


class MemoryRetriever:
    """只读检索；每次操作都由调用方提供可信的 MemoryScope。"""

    def __init__(self, database, *, document_root: str | Path | None = None,
                 allowed_document_paths: Iterable[str | Path] = (), documents: MarkdownDocuments | None = None,
                 now_ms: Callable[[], int] | None = None,
                 experience_config: MemoryExperienceConfig | None = None) -> None:
        self.db = database
        self._clock = now_ms
        self._experience_config = experience_config or MemoryExperienceConfig()
        self.document_root = Path(document_root).resolve() if document_root else None
        self.allowed_document_paths = frozenset(Path(path).resolve() for path in allowed_document_paths)
        self._documents = documents or MarkdownDocuments(self.document_root or Path.cwd(), database,
            policy_paths=self.allowed_document_paths, project_paths=self.allowed_document_paths)

    def recall(self, scope: MemoryScope, query: str, *, limit: int = 3,
               token_budget: int = 600) -> tuple[MemorySearchHit, ...]:
        self._require_scope(scope)
        if scope.scope_kind != "personal" or not query.strip() or limit <= 0 or token_budget <= 0:
            return ()
        terms = _terms(query[:512], specific=True)
        if not terms:
            return ()
        config = self._experience_config
        all_cards = {item.item_id: item for item in self._cards(scope, kind="reusable_experience")}
        ranked = []
        with self.db.read() as conn:
            unused = dict(conn.execute(
                "SELECT d.card_id,COUNT(*) FROM memory_card_decisions d "
                "JOIN memory_cards c ON c.id=d.card_id "
                "WHERE c.scope_key=? AND d.decision_type='usage' AND d.counted_at IS NOT NULL "
                "AND json_extract(d.result_json,'$.judgment.adopted')='no' "
                "AND d.model_key=COALESCE(json_extract(c.metadata_json,'$.stats_model_key'),'') "
                "AND d.card_revision>=COALESCE(json_extract(c.metadata_json,'$.stats_generation_start'),1) "
                "GROUP BY d.card_id", (scope.storage_key,)))
        for item in all_cards.values():
            if (not self._complete_experience(item) or not item.recall_enabled
                    or not self._valid_merged_card(item, all_cards)):
                continue
            keywords = tuple(keyword for keyword in item.keywords
                             if _specific_keyword(keyword) and _contains(query, keyword))
            if not keywords:
                continue
            applicability = getattr(item, "applicability", None) or ""
            condition_hits = sum(_contains(applicability, term) for term in terms)
            score = 10 * len(keywords) + 2 * condition_hits + item.priority / 100
            if config.enabled:
                maturity = (item.helpful_count - item.harmful_count) / (
                    item.helpful_count + item.harmful_count + config.maturity_prior)
                score *= 1 + config.maturity_weight * maturity
                if unused.get(item.item_id, 0) >= config.unused_threshold and not item.helpful_count:
                    score *= config.unused_weight
            ranked.append((score, _timestamp(item.updated_at), item.item_id, item, keywords))
        ranked.sort(key=lambda row: (-row[0], row[3].used_count, -row[1], row[2]))
        positions = {row[2]: index for index, row in enumerate(ranked)}
        # 压缩表示在最先命中的父卡位置试用；只有实际入选后才抑制父卡。
        def position(row):
            parents = row[3].metadata.get("merged_from", ())
            values = [positions[row[2]], *(positions[p["card_id"]] for p in parents
                       if p.get("card_id") in positions)]
            return (min(values), 0 if parents else 1, positions[row[2]])
        ranked.sort(key=position)
        hits: list[MemorySearchHit] = []
        entries = []
        suppressed = set()
        selected_ids = set()
        trial_selected = False
        for score, updated_at, _, item, keywords in ranked:
            if len(hits) >= min(limit, 3):
                break
            parents = {p["card_id"] for p in item.metadata.get("merged_from", ())}
            trial = bool(parents) and item.helpful_count + item.harmful_count < config.merge_minimum_decisive_count
            if item.item_id in suppressed or parents & selected_ids or (trial and trial_selected):
                continue
            content = self._experience_text(item)
            reason = "命中经验关键词：" + "、".join(keywords)
            entry = experience_entry(f"card:{item.item_id}", reason, content)
            if estimate_markdown_tokens(experience_block((*entries, entry))) > token_budget:
                continue
            entries.append(entry)
            selected_ids.add(item.item_id)
            suppressed.update(parents)
            trial_selected = trial_selected or trial
            hits.append(MemorySearchHit(
                reference=f"card:{item.item_id}", source="card",
                title=item.title or item.summary or item.slot_key or item.item_id,
                snippet=_excerpt(item.summary or item.content, terms),
                matched_keywords=keywords, evidence_refs=_evidence_refs(item),
                score=score, created_at=updated_at,
                kind="reusable_experience", content=content,
                match_reason=reason, item_id=item.item_id, revision=item.revision,
                card_snapshot=item.model_dump(mode="json"),
            ))
        return tuple(hits)

    def _valid_merged_card(self, item, cards) -> bool:
        parents = item.metadata.get("merged_from")
        if not parents:
            return True
        if item.metadata.get("merge_valid") is False or not isinstance(parents, (list, tuple)):
            return False
        from agent.core.memory.memory_embedding import experience_semantic_hash
        config = self._experience_config
        decisive = item.helpful_count + item.harmful_count
        maturity = (item.helpful_count-item.harmful_count)/(decisive+config.maturity_prior)
        if (decisive >= config.merge_minimum_decisive_count and maturity < config.merge_accept_maturity
                or item.used_count >= config.merge_trial_limit and decisive < config.merge_minimum_decisive_count):
            return False
        for ref in parents:
            if not isinstance(ref, dict):
                return False
            parent = cards.get(ref.get("card_id"))
            if (parent is None or parent.item_id == item.item_id
                    or parent.content_hash != ref.get("content_hash")
                    or ref.get("semantic_hash") and experience_semantic_hash(parent) != ref["semantic_hash"]):
                return False
        return True

    def search(self, scope: MemoryScope, query: str, *, source: str = "all",
               kind: str | None = None, limit: int = 10,
               offset: int = 0, source_message_ids: Iterable[str] | None = None) -> tuple[MemorySearchHit, ...]:
        self._require_scope(scope)
        if source not in {"all", "card", "history", "raw"}:
            raise ValueError("source 必须为 all/card/history/raw")
        if kind not in {None, "profile", "stable_preference", "ongoing_fact", "reusable_experience"}:
            raise ValueError("未知记忆卡片类别")
        query = query.strip()[:512]
        if source_message_ids is not None:
            if (not isinstance(source_message_ids, (list, tuple)) or not source_message_ids
                    or len(source_message_ids) > 50
                    or any(not isinstance(value, str) or not value or len(value) > 256
                           for value in source_message_ids)):
                raise ValueError("source_message_ids 必须是1到50个有效消息ID")
            if source not in {"all", "card"}:
                raise ValueError("来源消息反查只适用于卡片")
            return self._search_cards_by_source(scope, query, tuple(dict.fromkeys(source_message_ids)),
                                                kind, limit, offset)
        references = self._known_references(scope, query)
        if references or query.partition(":")[0] in {"card", "message", "tool", "document", "manual"}:
            hits = []
            for reference in references:
                page = self.read(scope, reference, limit=260)
                if page is None or source not in {"all", page.source}:
                    continue
                item = None
                if page.source == "card":
                    from agent.core.storage.sqlite.memory_store import read_user_memory
                    with self.db.read() as conn:
                        item = read_user_memory(conn, reference.partition(":")[2], scope_key=scope.storage_key)
                    if kind and item.kind.value != kind:
                        continue
                hits.append(MemorySearchHit(reference=reference, source=page.source,
                    title=(item.title or item.summary or reference) if item else reference, snippet=page.content,
                    matched_keywords=(query,), evidence_refs=_evidence_refs(item) if item else (),
                    score=1000000.0, created_at=0.0, match_reason="按ID精确命中",
                    kind=item.kind.value if item else None, item_id=item.item_id if item else None,
                    revision=item.revision if item else None))
            return tuple(hits[max(offset, 0):max(offset, 0) + max(1, min(limit, 50))])
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

    def _search_cards_by_source(self, scope, query, identities, kind, limit, offset):
        from .memory_store import read_user_memory
        now = self._now_ms()
        conditions = ["c.scope_key=?", "c.status='active'", "e.source_type='message'",
                      "e.source_id IN (" + ",".join("?" for _ in identities) + ")",
                      "(c.valid_from IS NULL OR c.valid_from<=?)", "(c.valid_until IS NULL OR c.valid_until>?)"]
        args = [scope.storage_key, *identities, now, now]
        if kind:
            conditions.append("c.kind=?")
            args.append(kind)
        if query:
            conditions.append("(instr(py_casefold(c.content),?)>0 OR EXISTS "
                              "(SELECT 1 FROM memory_terms t WHERE t.card_id=c.id AND instr(t.normalized_term,?)>0))")
            args.extend((query.casefold(), query.casefold()))
        args.extend((max(1, min(limit, 50)), max(0, offset)))
        with self.db.read() as conn:
            rows = conn.execute("SELECT DISTINCT c.id,c.updated_at FROM memory_evidence e "
                                "JOIN memory_cards c ON c.id=e.card_id WHERE " + " AND ".join(conditions)
                                + " ORDER BY c.updated_at DESC,c.id LIMIT ? OFFSET ?", args).fetchall()
            items = [read_user_memory(conn, row[0], scope_key=scope.storage_key) for row in rows]
        return tuple(MemorySearchHit(
            reference=f"card:{item.item_id}", source="card", title=item.title or item.summary or item.item_id,
            snippet=_excerpt(item.summary or item.content, _terms(query)), matched_keywords=(),
            evidence_refs=_evidence_refs(item), score=1.0, created_at=_timestamp(item.updated_at),
            kind=item.kind.value, match_reason="来源消息反查", item_id=item.item_id, revision=item.revision,
        ) for item in items)

    def read(self, scope: MemoryScope, reference: str, *, offset: int = 0,
             limit: int = 2000, query: str | None = None) -> MemoryReadPage | None:
        self._require_scope(scope)
        reference = reference.strip()
        prefix, separator, identity = reference.partition(":")
        if prefix not in {"card", "message", "tool", "document", "manual"}:
            references = self._known_references(scope, reference)
            pages = [self.read(scope, ref, offset=offset, limit=limit) for ref in references]
            pages = [page for page in pages if page is not None]
            if len(pages) > 1:
                raise ValueError("ID对应多类记录，请使用明确引用：" + "、".join(p.reference for p in pages))
            if not pages:
                return None
            return self.read(scope, pages[0].reference, offset=offset, limit=limit, query=query) if query else pages[0]
        if not separator or not identity or len(identity) > 256:
            return None
        context: tuple[dict, ...] = ()
        card_revision = None
        if prefix == "card":
            from agent.core.storage.sqlite.memory_store import read_user_memory
            with self.db.read() as conn:
                item = read_user_memory(conn, identity, scope_key=scope.storage_key)
            if (item is None or item.status.value != "active" or
                (item.valid_from and item.valid_from.timestamp() * 1000 > self._now_ms()) or
                (item.valid_until and item.valid_until.timestamp() * 1000 <= self._now_ms())):
                return None
            from agent.core.prompt.renderers import render_card
            body = render_card(item)
            source = "card"
            card_revision = item.revision
        elif prefix == "message":
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT m.session_id,m.seq_id,m.role,m.content_text,m.source_type "
                    "FROM messages m JOIN sessions s ON s.session_id=m.session_id "
                    "WHERE m.message_id=? AND s.scope_key=? AND m.role IN ('user','assistant','tool')",
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
            source = "raw" if row[2] == "tool" else "history"
        elif prefix == "tool":
            with self.db.read() as conn:
                row = conn.execute(
                    "SELECT c.tool_call_json FROM tool_calls c JOIN sessions s "
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
            try:
                body = self._documents.read_revision(scope, revision)
            except MemoryManualError:
                return None
            if body is None:
                return None
            source = "raw"
        else:
            return None
        offset, limit = max(0, offset), max(1, min(limit, 4000))
        if query:
            match = re.search(re.escape(query), body[offset:], re.IGNORECASE)
            if match is None:
                raise ValueError("当前引用在指定offset之后没有匹配内容")
            offset = max(offset, offset + match.start() - min(120, limit // 4))
        end = min(len(body), offset + limit)
        return MemoryReadPage(reference=reference, source=source, content=body[offset:end],
                              offset=offset, next_offset=end if end < len(body) else None,
                              total_chars=len(body), context=context, card_revision=card_revision)

    def _known_references(self, scope: MemoryScope, identity: str) -> tuple[str, ...]:
        """只按主键定位，不能把ID是否出现在正文中当成存在性判断。"""
        prefix, separator, value = identity.partition(":")
        if separator and prefix in {"card", "message", "tool", "document", "manual"}:
            return (identity,) if value else ()
        if not identity or len(identity) > 256:
            return ()
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT 'message:'||m.message_id FROM messages m JOIN sessions s ON s.session_id=m.session_id "
                "WHERE m.message_id=? AND s.scope_key=? AND m.role IN ('user','assistant','tool') "
                "UNION ALL SELECT 'tool:'||t.tool_call_id FROM tool_calls t JOIN sessions s ON s.session_id=t.session_id "
                "WHERE t.tool_call_id=? AND s.scope_key=? "
                "UNION ALL SELECT 'card:'||id FROM memory_cards WHERE id=? AND scope_key=?",
                (identity, scope.storage_key, identity, scope.storage_key, identity, scope.storage_key),
            ).fetchall()
        result = [row[0] for row in rows]
        if self._document(scope, identity):
            result.append("document:" + identity)
        return tuple(result)

    @staticmethod
    def _read_offset(content: str, terms: tuple[str, ...]) -> int:
        for term in sorted(terms, key=len, reverse=True):
            match = re.search(re.escape(term), content, re.IGNORECASE)
            if match:
                return match.start()
        return 0

    @staticmethod
    def _require_scope(scope: MemoryScope) -> None:
        if not isinstance(scope, MemoryScope):
            raise ValueError("缺少运行时 memory scope")

    def _now_ms(self) -> int:
        from agent.core.storage.sqlite.codec import milliseconds
        return self._clock() if self._clock is not None else milliseconds()

    def _cards(self, scope: MemoryScope, *, kind: str | None = None):
        from agent.core.storage.sqlite.memory_store import query_user_memories
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
                snippet=_excerpt(content, matches), read_offset=self._read_offset(content, matches), matched_keywords=matches, evidence_refs=(),
                score=float(len(matches) * 2), created_at=created_at / 1000,
                match_reason="对话原文命中",
            ))
        return hits

    def _search_tools(self, scope, terms):
        where = " OR ".join("instr(py_casefold(c.tool_call_json),?)>0" for _ in terms)
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT c.tool_call_id,c.tool_name,c.tool_call_json,c.created_at FROM tool_calls c "
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
                snippet=_excerpt(content, matches), read_offset=self._read_offset(content, matches), matched_keywords=matches, evidence_refs=(),
                score=float(len(matches) * 2), created_at=created_at / 1000,
                match_reason="工具原文命中",
            ))
        return hits

    @staticmethod
    def _tool_content(payload: dict) -> str:
        result = payload.get("result_text")
        if result:
            return str(result)
        if payload.get("error"):
            return str(payload["error"])
        structured = payload.get("result_data", payload.get("tool_call_result"))
        if structured is None:
            return ""
        from agent.core.prompt.renderers import _fields
        return "\n".join(_fields(structured))

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

    def _registered_content(self, scope: MemoryScope, layer: str, path: str,
                            registered_hash: str) -> str | None:
        try:
            return self._documents.read(scope, MemoryLayer(layer), path, persist=False).snapshot.content
        except (OSError, UnicodeError, ValueError):
            return None

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
                snippet=_excerpt(content, matches), read_offset=self._read_offset(content, matches), matched_keywords=matches, evidence_refs=(),
                score=float(len(matches)), created_at=updated_at / 1000,
                match_reason="已登记 Markdown 命中",
            ))
        return hits
