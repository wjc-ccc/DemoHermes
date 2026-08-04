"""
Agent/memory/episodic 包 - L2 情景记忆：对话轨迹的 SQLite 事实源。

组成：
- db.py            连接工厂（WAL/busy_timeout/foreign_keys + py_casefold SQL 函数）
- migrations.py    schema v1：sessions/slots/messages/tool_calls/review_jobs
- store.py         SqliteSessionStore（会话生命周期 + turn 持久化 + review outbox）
                   与 SqliteSessionArchiveReader（scope-safe LIKE 检索）
- recorder.py      占位（轨迹记录职责实际由 commit_turn 承担）
- fts.py           后置占位（ADR-8；首版用 LIKE，FTS5 待阈值触发）

与 Hot（精选偏好 Markdown）的区别：Hot 是提炼后的可读视图，本包是不可改的原始流水。
"""
