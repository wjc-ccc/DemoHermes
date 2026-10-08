"""结构化用户卡片的 SQLite 读写实现。

职责：统一保存卡片本体、经验字段、关键词、证据和版本快照。
边界：只执行持久化与版本校验；提案可信度和冲突裁决由上游负责。
关联：Committer、显式卡片修改和手册同步共用本文件；Retriever 只读卡片。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ...core.memory.memory_item import UserMemoryItem
from ...core.memory.memory_scope import MemoryScope
from ...core.memory.types.memory_source_types import EvidenceRef
from ...core.memory.types.card_types import ExperienceOutcome, UserCardKind, UserCardStatus
from .database import Database
from .codec import milliseconds, utcnow, dumps
from .errors import RevisionConflictError, IntegrityError


def _date(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc) if value is not None else None


def read_user_memory(conn, item_id: str, *, scope_key: str | None = None) -> UserMemoryItem | None:
    sql = "SELECT * FROM memory_cards WHERE id=?"
    args = [item_id]
    if scope_key is not None:
        sql += " AND scope_key=?"
        args.append(scope_key)
    cursor = conn.execute(sql, args)
    row = cursor.fetchone()
    if row is None:
        return None
    card = dict(zip((column[0] for column in cursor.description), row))
    experience_row = conn.execute(
        "SELECT applicability,action,outcome,limitations,outcome_status "
        "FROM memory_experiences WHERE card_id=?", (item_id,),
    ).fetchone()
    experience = dict(zip(("applicability", "action", "outcome", "limitations",
                           "outcome_status"), experience_row)) if experience_row else {}
    terms = tuple(value for (value,) in conn.execute(
        "SELECT term FROM memory_terms WHERE card_id=? ORDER BY rowid", (item_id,)))
    refs = tuple(EvidenceRef(
        source_type=kind, source_id=identity, source_revision=revision,
        source_path=path, source_hash=digest, excerpt=quote, observed_at=_date(observed),
    ) for kind, identity, revision, path, digest, quote, observed in conn.execute(
        "SELECT source_type,source_id,source_revision,source_path,source_hash,excerpt,observed_at "
        "FROM memory_evidence WHERE card_id=? ORDER BY rowid", (item_id,)))
    scope = MemoryScope(
        organization_id=card["organization_id"], tenant_id=card["tenant_id"],
        user_id=card["user_id"], project_id=card["project_id"],
    )
    return UserMemoryItem(
        item_id=card["id"], kind=card["kind"], slot_key=card["slot_key"],
        scope=scope, source_type=card["source_type"], source_ref=card["source_ref"],
        title=card["title"], summary=card["summary"], content=card["content"],
        metadata=json.loads(card["metadata_json"]), status=card["status"],
        priority=card["priority"], confidence=card["confidence"],
        source_authority=card["source_authority"],
        valid_from=_date(card["valid_from"]), valid_until=_date(card["valid_until"]),
        supersedes_item_id=card["supersedes_card_id"], revision=card["revision"],
        content_hash=card["content_hash"], token_count=card["token_count"],
        created_at=_date(card["created_at"]), updated_at=_date(card["updated_at"]),
        last_confirmed_at=_date(card["last_confirmed_at"]),
        keywords=terms, evidence=refs, **experience,
    )


def query_user_memories(conn, where: str, args: tuple | list = (), *,
                        order: str = "updated_at DESC,id", limit: int | None = None):
    sql = "SELECT id FROM memory_cards WHERE " + where + " ORDER BY " + order
    params = list(args)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    ids = tuple(row[0] for row in conn.execute(sql, params))
    return tuple(read_user_memory(conn, identity) for identity in ids)


def put_user_memory(conn, item: UserMemoryItem, *, expected_revision: int = 0,
                    now: datetime | None = None) -> UserMemoryItem:
    item = UserMemoryItem.model_validate(item.model_dump())
    row = conn.execute("SELECT revision,scope_key FROM memory_cards WHERE id=?",
                       (item.item_id,)).fetchone()
    actual = row[0] if row else 0
    if actual != expected_revision or item.revision != expected_revision + 1:
        raise RevisionConflictError("memory revision changed")
    if row and row[1] != item.scope.storage_key:
        raise IntegrityError("memory scope cannot change")
    now = now or utcnow()
    seen = set()
    keywords = []
    for keyword in item.keywords:
        keyword = keyword.strip()
        if keyword and keyword.casefold() not in seen:
            seen.add(keyword.casefold())
            keywords.append(keyword)
    canonical_now = _date(milliseconds(now))
    canonical_refs = tuple(ref.model_copy(update={
        "observed_at": _date(milliseconds(ref.observed_at)) if ref.observed_at else None,
    }) for ref in item.evidence)
    stored = item.model_copy(update={
        "created_at": _date(milliseconds(item.created_at or now)),
        "updated_at": canonical_now,
        "valid_from": _date(milliseconds(item.valid_from)) if item.valid_from else None,
        "valid_until": _date(milliseconds(item.valid_until)) if item.valid_until else None,
        "last_confirmed_at": _date(milliseconds(item.last_confirmed_at))
        if item.last_confirmed_at else None,
        "keywords": tuple(keywords), "evidence": canonical_refs,
    })
    scope = stored.scope
    values = dict(
        id=stored.item_id, scope_key=scope.storage_key,
        organization_id=scope.organization_id, tenant_id=scope.tenant_id,
        user_id=scope.user_id, project_id=scope.project_id,
        kind=stored.kind.value, slot_key=stored.slot_key, title=stored.title,
        summary=stored.summary or stored.content, content=stored.content,
        source_type=stored.source_type, source_ref=stored.source_ref,
        metadata_json=dumps(dict(stored.metadata)), status=stored.status.value,
        priority=stored.priority, confidence=stored.confidence,
        source_authority=stored.source_authority,
        valid_from=milliseconds(stored.valid_from) if stored.valid_from else None,
        valid_until=milliseconds(stored.valid_until) if stored.valid_until else None,
        supersedes_card_id=stored.supersedes_item_id, revision=stored.revision,
        content_hash=stored.content_hash, token_count=stored.token_count,
        created_at=milliseconds(stored.created_at), updated_at=milliseconds(canonical_now),
        last_confirmed_at=milliseconds(stored.last_confirmed_at) if stored.last_confirmed_at else None,
    )
    columns = tuple(values)
    conn.execute("INSERT INTO memory_cards (" + ",".join(columns) + ") VALUES (" +
                 ",".join("?" for _ in columns) + ") ON CONFLICT(id) DO UPDATE SET " +
                 ",".join(c + "=excluded." + c for c in columns if c != "id"),
                 tuple(values.values()))
    conn.execute("DELETE FROM memory_experiences WHERE card_id=?", (stored.item_id,))
    if stored.kind is UserCardKind.REUSABLE_EXPERIENCE:
        conn.execute("INSERT INTO memory_experiences VALUES (?,?,?,?,?,?)", (
            stored.item_id, stored.applicability or "", stored.action or "",
            stored.outcome or "", stored.limitations or "",
            (stored.outcome_status or ExperienceOutcome.OBSERVED).value,
        ))
    conn.execute("DELETE FROM memory_terms WHERE card_id=?", (stored.item_id,))
    conn.executemany("INSERT INTO memory_terms VALUES (?,?,?)", (
        (stored.item_id, word, word.casefold()) for word in keywords))
    conn.execute("DELETE FROM memory_evidence WHERE card_id=?", (stored.item_id,))
    conn.executemany("INSERT INTO memory_evidence VALUES (?,?,?,?,?,?,?,?)", (
        (stored.item_id, ref.source_type, ref.source_id, ref.source_revision,
         ref.source_path, ref.source_hash, ref.excerpt,
         milliseconds(ref.observed_at) if ref.observed_at else None)
        for ref in stored.evidence))
    conn.execute("INSERT INTO memory_card_versions VALUES (?,?,?,?)", (
        stored.item_id, stored.revision, stored.model_dump_json(), milliseconds(canonical_now)))
    return stored


def _legacy_snapshot(raw: str) -> UserMemoryItem:
    data = json.loads(raw)
    if "kind" in data:
        return UserMemoryItem.model_validate(data)
    old_type = data.get("type")
    kind = data.get("category") or (
        "reusable_experience" if old_type == "reusable_experience" else
        "profile" if old_type in {"identity", "capability", "relationship"} else
        "stable_preference" if old_type in {"preference", "tenant_config"} else "ongoing_fact")
    old_key = data.get("normalized_key", "")
    key = ("stable_preference:assistant:response_language"
           if old_key in {"preference.response_language", "user.response.language", "language.preference"}
           else "stable_preference:assistant:response_detail"
           if old_key in {"preference.response_detail", "user.response.detail", "response.detail"}
           else "profile:user:display_name" if old_key == "identity.display_name"
           else f"legacy:{kind}:{old_key}")
    data["kind"] = kind
    data["slot_key"] = None if kind == "reusable_experience" else key
    data["status"] = {"proposed": "quarantined", "rejected": "quarantined"}.get(
        data.get("status"), data.get("status", "quarantined"))
    data["outcome_status"] = "observed" if kind == "reusable_experience" else None
    for redundant in ("type", "category", "memory_kind", "normalized_key"):
        data.pop(redundant, None)
    return UserMemoryItem.model_validate(data)


class SqliteMemoryStore:
    def __init__(self, db: Database):
        self.db = db

    def put(self, item: UserMemoryItem, *, expected_revision: int = 0) -> UserMemoryItem:
        with self.db.write() as conn:
            return put_user_memory(conn, item, expected_revision=expected_revision)

    def get(self, item_id: str) -> UserMemoryItem | None:
        with self.db.read() as conn:
            return read_user_memory(conn, item_id)

    def history(self, item_id: str, *, limit: int = 100, offset: int = 0):
        with self.db.read() as conn:
            rows = conn.execute("SELECT snapshot_json FROM memory_card_versions WHERE card_id=? "
                                "ORDER BY revision DESC LIMIT ? OFFSET ?",
                                (item_id, max(1,min(limit,100)), max(0,offset))).fetchall()
        return tuple(_legacy_snapshot(raw) for (raw,) in rows)

    def search(self, scope, query: str = "", *, limit: int = 50, offset: int = 0,
               kind: str | None = None, statuses: tuple[str, ...] = ('active',)):
        if not statuses:
            return ()
        statuses = tuple(UserCardStatus(status).value for status in statuses)
        kind = getattr(kind, 'value', kind)
        conditions = ["scope_key=?", "status IN (" + ",".join("?" for _ in statuses) + ")",
                      "(valid_from IS NULL OR valid_from<=?)", "(valid_until IS NULL OR valid_until>?)"]
        now = milliseconds()
        values: list = [scope.storage_key, *statuses, now, now]
        if kind is not None:
            conditions.append("kind=?")
            values.append(kind)
        if query:
            conditions.append("(instr(py_casefold(summary),py_casefold(?))>0 OR "
                              "EXISTS (SELECT 1 FROM memory_terms t WHERE t.card_id=memory_cards.id "
                              "AND instr(t.normalized_term,py_casefold(?))>0))")
            values.extend((query, query))
        values.extend((max(1, min(limit, 100)), max(0, offset)))
        with self.db.read() as conn:
            ids = tuple(row[0] for row in conn.execute(
                "SELECT id FROM memory_cards WHERE " + " AND ".join(conditions) +
                " ORDER BY priority DESC,updated_at DESC,id LIMIT ? OFFSET ?", tuple(values)))
            return tuple(read_user_memory(conn, identity) for identity in ids)

    def soft_delete(self, item_id: str, *, expected_revision: int):
        with self.db.write() as conn:
            item = read_user_memory(conn, item_id)
            if item is None:
                raise LookupError(item_id)
            return put_user_memory(conn, item.model_copy(update={
                "status": UserCardStatus.DELETED, "revision": expected_revision+1}),
                expected_revision=expected_revision)
