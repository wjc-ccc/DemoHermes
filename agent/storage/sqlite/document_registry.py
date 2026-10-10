"""Markdown 权威文档的派生元数据注册表。"""
from __future__ import annotations

import hashlib

from agent.types.memory.snapshot import MarkdownBookSnapshot
from .database import Database
from .codec import milliseconds


class SqliteDocumentRegistry:
    def __init__(self, db: Database, *, tokenizer_name: str = "unknown") -> None:
        self.db = db
        self._tokenizer_name = tokenizer_name

    def register(self, snapshot: MarkdownBookSnapshot) -> str:
        scope = snapshot.scope
        identity = "|".join(
            (
                snapshot.layer.value,
                snapshot.path,
                scope.organization_id or "",
                scope.tenant_id or "",
                scope.user_id or "",
                scope.project_id or "",
            )
        )
        document_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        now = milliseconds()
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO context_documents "
                "(id, layer, type, scope_key, organization_id, tenant_id, user_id, project_id, path, "
                "content_hash, token_count, tokenizer_name, last_loaded_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET content_hash = excluded.content_hash, "
                "token_count = excluded.token_count, tokenizer_name = excluded.tokenizer_name, "
                "last_loaded_at = excluded.last_loaded_at, updated_at = excluded.updated_at",
                (
                    document_id,
                    snapshot.layer.value,
                    snapshot.type,
                    scope.storage_key,
                    scope.organization_id,
                    scope.tenant_id,
                    scope.user_id,
                    scope.project_id,
                    snapshot.path,
                    snapshot.content_hash,
                    snapshot.token_count,
                    self._tokenizer_name,
                    now,
                    now,
                    now,
                ),
            )
        return document_id
