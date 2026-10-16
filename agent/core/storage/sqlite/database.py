"""SQLite 连接工厂；每个调用方获取独立短连接。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


def _py_casefold(value) -> str:
    return "" if value is None else str(value).casefold()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("py_casefold", 1, _py_casefold)
    return conn


class Database:
    """共享短写事务锁；读连接只读，连接不跨线程共享。迁移由 RuntimeStorage 调用。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._write_lock = threading.RLock()

    @contextmanager
    def read(self):
        conn = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5.0)
        conn.create_function('py_casefold', 1, _py_casefold)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self):
        with self._write_lock:
            conn = connect(self.path)
            try:
                conn.execute('BEGIN IMMEDIATE')
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
