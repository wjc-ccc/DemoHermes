from __future__ import annotations

from agent.storage.sqlite import RuntimeStorage
from agent.core.memory import MemoryScope
from agent.storage.sqlite import SqliteMemoryStore
from scripts.migrate_legacy_memory_markers import migrate_document


def test_legacy_marker_migration_is_dry_run_by_default(tmp_path):
    document = tmp_path / "MEMORY.md"
    original = (
        "# USER\n\n<!-- memory-entry:e_1 -->\n先给结论\n"
        "<!-- /memory-entry:e_1 -->\n"
    )
    document.write_text(original, encoding="utf-8")

    report = migrate_document(document)

    assert report.entry_count == 1
    assert report.rewritten is False
    assert document.read_text(encoding="utf-8") == original


def test_apply_imports_card_backs_up_and_removes_markers(tmp_path):
    memory_dir = tmp_path / "scope"
    memory_dir.mkdir()
    document = memory_dir / "MEMORY.md"
    document.write_text(
        "# USER\n\n<!-- memory-entry:e_1 -->\n[playbook] 先验证再提交\n"
        "<!-- /memory-entry:e_1 -->\n",
        encoding="utf-8",
    )
    (memory_dir / "scope.json").write_text(
        '{"tenant_id":"cli","user_id":"u1"}', encoding="utf-8"
    )
    db = tmp_path / "memory.db"

    report = migrate_document(document, db_path=db, apply=True)

    assert report.rewritten is True
    assert "memory-entry" not in document.read_text(encoding="utf-8")
    assert document.with_suffix(".md.legacy-marker.bak").exists()
    item = RuntimeStorage.open(db).memory.get(report.imported_item_ids[0])
    assert item is not None
    assert item.scope == MemoryScope(tenant_id="cli", user_id="u1")
    assert item.type == "reusable_experience"
    assert item.status.value == "proposed"
