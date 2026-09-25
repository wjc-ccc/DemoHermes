from __future__ import annotations

from agent.storage.sqlite import connect
from agent.storage.sqlite.migrations import migrate


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

    assert versions == ["001_runtime"]
    assert len(tables) == 17
    assert {"turns", "jobs", "memory_items", "session_state_items"} <= tables
