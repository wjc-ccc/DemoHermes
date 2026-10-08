"""分页筛选必须作用于整张表，而非当前页。"""
import sqlite3

import pytest

from agent.storage.sqlite.catalog import CatalogError, read_table_page


def test_categorical_filters_apply_before_count_and_pagination(tmp_path):
    path = tmp_path / "inspect.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE examples (id INTEGER PRIMARY KEY, category TEXT, status TEXT, created_at INTEGER)")
        conn.executemany(
            "INSERT INTO examples (category, status, created_at) VALUES (?, ?, ?)",
            [("a", "active", 1), ("b", "active", 2), ("a", "done", 3),
             (None, "active", 4), ("a", "active", 5)],
        )
    databases = {"test": path}

    page = read_table_page(databases, "test", "examples", limit=1, offset=1,
                           filters={"category": "a", "status": "active"})
    assert page["total"] == 2
    assert [row["id"] for row in page["rows"]] == [5]
    assert page["filter_options"]["category"] == [None, "a", "b"]
    assert "created_at" not in page["filter_options"]
    assert "id" not in page["filter_options"]

    empty = read_table_page(databases, "test", "examples", limit=10, offset=0,
                            filters={"category": None})
    assert empty["total"] == 1
    assert empty["rows"][0]["id"] == 4
    with pytest.raises(CatalogError, match="unknown filter column"):
        read_table_page(databases, "test", "examples", limit=10, offset=0,
                        filters={"created_at": 1})
