"""episodic SQLite 连接工厂（7.23 方案 §6.1）。

约束：
- 每个连接都执行 foreign_keys / WAL / busy_timeout 三条 PRAGMA；
- 禁止跨线程共享同一个 connection——多线程经本工厂各自取连接；
- 每次 save/reset 是短事务，连接即用即关。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _py_casefold(value) -> str:
    """SQL 函数：Unicode casefold——大小写匹配不依赖 SQLite build 的默认 collation（§6.4）。"""
    return "" if value is None else str(value).casefold()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开一个已按 §6.1 配置好的新连接；调用方负责关闭。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("py_casefold", 1, _py_casefold)
    return conn
