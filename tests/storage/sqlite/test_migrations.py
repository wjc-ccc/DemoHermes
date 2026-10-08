from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent.core.memory import MemoryScope, UserMemoryItem
from agent.core.memory.memory_item import content_sha256
from agent.core.memory.types.memory_source_types import EvidenceRef
from agent.storage.sqlite import RuntimeStorage, connect
from agent.storage.sqlite.migrations import migrate
from agent.storage.sqlite.memory_store import put_user_memory
from agent.storage.sqlite.job_store import enqueue_job


def test_migrations_create_all_schema_versions(tmp_path) -> None:
    conn = connect(tmp_path / "agent.db")
    try:
        migrate(conn)
        migrate(conn)
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()

    assert versions == ["001_runtime", "002_memory_cycle", "003_manual_sync_and_legacy",
                        "004_atomic_cards", "005_memory_cleanup", "006_memory_eval_datasets"]
    assert {"turns", "jobs", "memory_cards", "memory_experiences", "memory_evidence",
            "memory_terms", "memory_card_versions", "memory_conflicts",
            "memory_review_turns", "memory_document_versions"} <= tables
    assert "memory_items" not in tables
    assert "memory_document_sync" not in tables
    assert "session_state_items" not in tables


def test_upgrade_from_001_preserves_proposed_card_and_marks_it_for_revalidation(tmp_path):
    path = tmp_path / "agent.db"
    sql = (Path(__file__).resolve().parents[3] / "agent" / "storage" / "sqlite" /
           "schema" / "001_runtime.sql").read_text(encoding="utf-8")
    conn = connect(path)
    try:
        conn.executescript(sql)
        conn.execute(
            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,checksum TEXT NOT NULL,applied_at INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO schema_migrations VALUES (?,?,?)",
                     ("001_runtime", hashlib.sha256(sql.encode()).hexdigest(), 1))
        scope = MemoryScope(tenant_id="cli", user_id="u1")
        content = "以后用中文"
        card = {"item_id": "old-proposed", "layer": "user", "type": "preference",
                "source_type": "memory_extraction", "source_ref": "old-message",
                "scope": scope.model_dump(), "content": content, "summary": content,
                "content_hash": content_sha256(content),
                "normalized_key": "preference.response_language",
                "source_authority": "model_inferred", "status": "proposed", "metadata": {}}
        conn.execute(
            "INSERT INTO memory_items(id,layer,type,scope_key,tenant_id,user_id,normalized_key,"
            "summary,content_json,keywords_json,evidence_json,status,priority,confidence,"
            "source_authority,revision,content_hash,token_count,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (card["item_id"], "user", card["type"], scope.storage_key, scope.tenant_id,
             scope.user_id, card["normalized_key"], card["summary"], json.dumps(card),
             "[]", "[]", "proposed", 50, 0.5, card["source_authority"], 1,
             card["content_hash"], 0, 1, 1),
        )
        conn.commit()
        migrate(conn)
        assert conn.execute(
            "SELECT kind,status,slot_key FROM memory_cards WHERE id='old-proposed'"
        ).fetchone() == ("stable_preference", "quarantined",
                         "stable_preference:assistant:response_language")
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_items'").fetchone() is None
    finally:
        conn.close()


def test_upgrade_deduplicates_old_aliases_and_keeps_versions(tmp_path):
    path = tmp_path / "agent.db"
    sql = (Path(__file__).resolve().parents[3] / "agent" / "storage" / "sqlite" /
           "schema" / "001_runtime.sql").read_text(encoding="utf-8")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    conn = connect(path)
    try:
        conn.executescript(sql)
        conn.execute("CREATE TABLE schema_migrations("
                     "version TEXT PRIMARY KEY,checksum TEXT NOT NULL,applied_at INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_migrations VALUES (?,?,?)",
                     ("001_runtime", hashlib.sha256(sql.encode()).hexdigest(), 1))
        for identity, key, language, updated in (
            ("old-zh", "language.preference", "中文", 1),
            ("new-en", "preference.response_language", "英文", 2),
        ):
            claim = f"以后用{language}回答"
            payload = {"item_id": identity, "layer": "user", "type": "preference",
                       "source_type": "memory_extraction", "source_ref": identity,
                       "scope": scope.model_dump(), "content": claim, "summary": claim,
                       "content_hash": content_sha256(claim), "normalized_key": key,
                       "source_authority": "user_explicit", "status": "active",
                       "revision": 1}
            raw = json.dumps(payload, ensure_ascii=False)
            conn.execute(
                "INSERT INTO memory_items(id,layer,type,scope_key,tenant_id,user_id,"
                "normalized_key,summary,content_json,keywords_json,evidence_json,status,"
                "priority,confidence,source_authority,revision,content_hash,token_count,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, "user", "preference", scope.storage_key, scope.tenant_id,
                 scope.user_id, key, claim, raw, "[]", "[]", "active", 50, 1.0,
                 "user_explicit", 1, payload["content_hash"], 10, updated, updated))
            conn.execute("INSERT INTO memory_item_versions VALUES (?,?,?,?)",
                         (identity, 1, raw, updated))
        conn.commit()
        migrate(conn)
        assert conn.execute("SELECT id,status FROM memory_cards ORDER BY id").fetchall() == [
            ("new-en", "active"), ("old-zh", "conflicted")]
        assert conn.execute("SELECT COUNT(*) FROM memory_card_versions").fetchone()[0] == 2
    finally:
        conn.close()
    assert RuntimeStorage.open(path).memory.history("new-en")[0].content == "以后用英文回答"


def test_005_quarantines_manual_only_card_and_cancels_sync_job(tmp_path):
    path = tmp_path / "agent.db"
    schema = Path(__file__).resolve().parents[3] / "agent" / "storage" / "sqlite" / "schema"
    conn = connect(path)
    try:
        conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,"
                     "checksum TEXT NOT NULL,applied_at INTEGER NOT NULL)")
        for name in ("001_runtime", "002_memory_cycle", "003_manual_sync_and_legacy",
                     "004_atomic_cards"):
            sql = (schema / f"{name}.sql").read_text(encoding="utf-8")
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?,?,?)",
                         (name, hashlib.sha256(sql.encode()).hexdigest(), 1))
            conn.commit()
        scope = MemoryScope(tenant_id="cli", user_id="u1")
        content = "以后用中文回答"
        put_user_memory(conn, UserMemoryItem(
            item_id="manual-card", kind="stable_preference",
            slot_key="stable_preference:assistant:response_language",
            source_type="manual_document", source_ref="1", scope=scope,
            summary=content, content=content, content_hash=content_sha256(content),
            source_authority="manual",
            evidence=(EvidenceRef(source_type="manual", source_id="1"),),
        ))
        enqueue_job(conn, job_type="memory.manual_sync", handler_version="legacy",
                    input={"revision": 1}, dedupe_key="old-sync")
        conn.commit()
        migrate(conn)
        assert conn.execute("SELECT status,revision FROM memory_cards WHERE id='manual-card'").fetchone() == (
            "quarantined", 2)
        assert conn.execute("SELECT COUNT(*) FROM memory_card_versions WHERE card_id='manual-card'").fetchone()[0] == 2
        assert conn.execute("SELECT status FROM jobs WHERE dedupe_key='old-sync'").fetchone()[0] == "cancelled"
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_document_sync'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='session_workspaces'").fetchone() is None
    finally:
        conn.close()
