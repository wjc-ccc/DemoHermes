"""Versioned user memory cards and their embedded evidence."""
from __future__ import annotations

from datetime import datetime
from ...core.memory.memory_item import UserMemoryItem, UserMemoryStatus
from .database import Database
from .codec import milliseconds, utcnow, dumps
from .errors import RevisionConflictError, IntegrityError


def put_user_memory(conn, item: UserMemoryItem, *, expected_revision: int = 0,
                    now: datetime | None = None) -> UserMemoryItem:
    item = UserMemoryItem.model_validate(item.model_dump())
    row = conn.execute("SELECT revision,content_json FROM memory_items WHERE id=?", (item.item_id,)).fetchone()
    actual = row[0] if row else 0
    if actual != expected_revision or item.revision != expected_revision + 1:
        raise RevisionConflictError("memory revision changed")
    if row and UserMemoryItem.model_validate_json(row[1]).scope.storage_key != item.scope.storage_key:
        raise IntegrityError("memory scope cannot change")
    now = now or utcnow()
    seen = set()
    keywords = []
    for keyword in item.keywords:
        keyword = keyword.strip()
        if keyword and keyword.casefold() not in seen:
            seen.add(keyword.casefold())
            keywords.append(keyword)
    stored = item.model_copy(update={"created_at": item.created_at or now, "updated_at": now,
                                     "keywords": tuple(keywords)})
    scope = stored.scope
    values = dict(
        id=stored.item_id, layer=stored.layer.value, type=stored.type,
        memory_kind=stored.memory_kind.value if stored.memory_kind else None,
        organization_id=scope.organization_id, tenant_id=scope.tenant_id,
        user_id=scope.user_id, project_id=scope.project_id, scope_key=scope.storage_key,
        normalized_key=stored.normalized_key, title=stored.title,
        summary=stored.summary or stored.content, content_json=stored.model_dump_json(),
        keywords_json=dumps(stored.keywords),
        evidence_json=dumps([e.model_dump(mode="json") for e in stored.evidence]),
        status=stored.status.value, priority=stored.priority, confidence=stored.confidence,
        source_authority=stored.source_authority,
        valid_from=milliseconds(stored.valid_from) if stored.valid_from else None,
        valid_until=milliseconds(stored.valid_until) if stored.valid_until else None,
        supersedes_item_id=stored.supersedes_item_id, revision=stored.revision,
        content_hash=stored.content_hash, token_count=stored.token_count,
        created_at=milliseconds(stored.created_at), updated_at=milliseconds(now),
        last_confirmed_at=milliseconds(stored.last_confirmed_at) if stored.last_confirmed_at else None)
    columns = tuple(values)
    conn.execute("INSERT INTO memory_items (" + ",".join(columns) + ") VALUES (" +
                 ",".join("?" for _ in columns) + ") ON CONFLICT(id) DO UPDATE SET " +
                 ",".join(c + "=excluded." + c for c in columns if c != "id"), tuple(values.values()))
    conn.execute("INSERT INTO memory_item_versions VALUES (?,?,?,?)",
                 (stored.item_id, stored.revision, stored.model_dump_json(), milliseconds(now)))
    return stored


class SqliteMemoryStore:
    def __init__(self, db: Database):
        self.db = db

    def put(self, item: UserMemoryItem, *, expected_revision: int = 0) -> UserMemoryItem:
        with self.db.write() as conn:
            return put_user_memory(conn, item, expected_revision=expected_revision)

    def get(self, item_id: str) -> UserMemoryItem | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT content_json FROM memory_items WHERE id=?", (item_id,)).fetchone()
        return UserMemoryItem.model_validate_json(row[0]) if row else None

    def history(self, item_id: str, *, limit: int = 100, offset: int = 0):
        with self.db.read() as conn:
            rows = conn.execute("SELECT snapshot_json FROM memory_item_versions WHERE memory_item_id=? "
                                "ORDER BY revision DESC LIMIT ? OFFSET ?",
                                (item_id, max(1,min(limit,100)), max(0,offset))).fetchall()
        return tuple(UserMemoryItem.model_validate_json(r[0]) for r in rows)

    def search(self, scope, query: str = "", *, limit: int = 50, offset: int = 0):
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT content_json FROM memory_items WHERE scope_key=? AND status IN ('active','proposed') "
                "AND (instr(py_casefold(summary),py_casefold(?))>0 OR "
                "EXISTS (SELECT 1 FROM json_each(keywords_json) WHERE instr(py_casefold(value),py_casefold(?))>0)) "
                "ORDER BY priority DESC,updated_at DESC,id LIMIT ? OFFSET ?",
                (scope.storage_key,query,query,max(1,min(limit,100)),max(0,offset))).fetchall()
        return tuple(UserMemoryItem.model_validate_json(r[0]) for r in rows)

    def soft_delete(self, item_id: str, *, expected_revision: int):
        with self.db.write() as conn:
            row = conn.execute("SELECT content_json FROM memory_items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise LookupError(item_id)
            item = UserMemoryItem.model_validate_json(row[0])
            return put_user_memory(conn, item.model_copy(update={
                "status": UserMemoryStatus.DELETED, "revision": expected_revision+1}),
                expected_revision=expected_revision)
