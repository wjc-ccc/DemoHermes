"""
Agent/memory/episodic 包 - L2 情景记忆：对话轨迹的 SQLite 事实源。

组成：
- db.py       连接工厂（WAL/busy_timeout/foreign_keys + py_casefold）
- schema.py   逐表 CREATE TABLE IF NOT EXISTS（不做版本迁移）
- store.py    SqliteSessionStore + SqliteSessionArchiveReader
"""
