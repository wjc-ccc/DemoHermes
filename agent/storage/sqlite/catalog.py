"""只读扫已有 SQLite 文件的表目录与分页行。不建库、不写 WAL。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Mapping
from .catalog_metadata import TABLES, field_description


class CatalogError(ValueError):
    """库名或表名不在已打开的目录里。"""


def list_catalog(databases: Mapping[str, Path]) -> dict:
    """返回每个已登记库的用户表及其字段。文件还不存在时表列表为空。"""
    result = []
    for name, path in databases.items():
        tables = _tables(Path(path)) if Path(path).is_file() else []
        result.append({"name": name, "tables": tables})
    return {"databases": result}


def read_table_page(
    databases: Mapping[str, Path],
    database: str,
    table: str,
    *,
    limit: int,
    offset: int,
) -> dict:
    """按库名和表名分页读取。名字必须先出现在目录里。"""
    path = databases.get(database)
    if path is None or not Path(path).is_file():
        raise CatalogError("unknown database")
    columns = _columns(Path(path), table)
    if columns is None:
        raise CatalogError("unknown table")
    bounded_limit = max(1, min(int(limit), 100))
    bounded_offset = max(0, int(offset))
    quoted = _quote(table)
    conn = open_readonly(Path(path))
    try:
        conn.execute("BEGIN")
        total = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        cursor = conn.execute(
            f"SELECT * FROM {quoted} ORDER BY rowid LIMIT ? OFFSET ?",
            (bounded_limit, bounded_offset),
        )
        names = [item[0] for item in cursor.description]
        rows = [
            {column: _cell(value) for column, value in zip(names, row)}
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()
    return {
        "database": database,
        "table": table,
        "columns": columns,
        "rows": rows,
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


def _tables(path: Path) -> list[dict]:
    conn = open_readonly(path)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        return [
            {"name": name, "columns": _columns(path, name, conn),
             "group": TABLES.get(name, ("其他", ""))[0],
             "description": TABLES.get(name, ("其他", ""))[1]}
            for name in names
        ]
    finally:
        conn.close()


def _columns(
    path: Path, table: str, conn: sqlite3.Connection | None = None
) -> list[dict] | None:
    own = conn is None
    if own:
        if not path.is_file():
            return None
        conn = open_readonly(path)
    assert conn is not None
    try:
        known = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            )
        }
        if table not in known or table.startswith("sqlite_"):
            return None
        return [
            {
                "name": row[1],
                "type": row[2] or "",
                "description": field_description(row[1]),
                "notnull": bool(row[3]),
                "pk": bool(row[5]),
            }
            for row in conn.execute(f"PRAGMA table_info({_quote(table)})")
        ]
    finally:
        if own:
            conn.close()


def open_readonly(path: Path) -> sqlite3.Connection:
    """以只读 URI 打开已有文件。文件不存在时由 SQLite 抛错，不会新建。"""
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _cell(value: object) -> object:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
