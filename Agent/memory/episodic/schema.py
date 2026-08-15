"""episodic SQLite 表定义——直接 CREATE TABLE，不做版本迁移。

需要改表结构时再单独加迁移；日常启动只执行本文件的建表语句。
"""

# 会话头：payload_json 不含 messages/tool_calls（子表为事实源）
SQL_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id      TEXT PRIMARY KEY,
  session_key     TEXT NOT NULL,
  scope_kind      TEXT NOT NULL CHECK(scope_kind IN ('personal', 'group')),
  scope_key       TEXT NOT NULL,
  status          TEXT NOT NULL,
  channel         TEXT NOT NULL,
  author_id       TEXT NOT NULL,
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  last_active_at  REAL NOT NULL,
  payload_json    TEXT NOT NULL,
  schema_version  INTEGER NOT NULL DEFAULT 1
)
"""

SQL_SESSION_SLOTS = """
CREATE TABLE IF NOT EXISTS session_slots (
  session_key       TEXT PRIMARY KEY,
  active_session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
  scope_kind        TEXT NOT NULL CHECK(scope_kind IN ('personal', 'group')),
  scope_key         TEXT NOT NULL,
  updated_at        REAL NOT NULL
)
"""

SQL_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
  message_id      TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
  seq_id          INTEGER NOT NULL,
  role            TEXT NOT NULL,
  content_text    TEXT NOT NULL,
  created_at      REAL NOT NULL,
  payload_json    TEXT NOT NULL,
  schema_version  INTEGER NOT NULL DEFAULT 1,
  UNIQUE(session_id, seq_id)
)
"""

SQL_TOOL_CALLS = """
CREATE TABLE IF NOT EXISTS tool_calls (
  tool_call_id    TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
  trajectory_id   TEXT NOT NULL,
  tool_name       TEXT NOT NULL,
  status          TEXT NOT NULL,
  created_at      REAL NOT NULL,
  payload_json    TEXT NOT NULL,
  schema_version  INTEGER NOT NULL DEFAULT 1
)
"""

SQL_REVIEW_JOBS = """
CREATE TABLE IF NOT EXISTS review_jobs (
  turn_id          TEXT PRIMARY KEY,
  session_id       TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
  scope_kind       TEXT NOT NULL CHECK(scope_kind IN ('personal', 'group')),
  scope_key        TEXT NOT NULL,
  status           TEXT NOT NULL CHECK(status IN ('pending', 'running', 'done', 'failed')),
  attempts         INTEGER NOT NULL DEFAULT 0,
  available_at     REAL NOT NULL,
  lease_until      REAL,
  claimed_by       TEXT,
  payload_json     TEXT NOT NULL,
  last_error       TEXT
)
"""

SQL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_scope ON sessions(scope_kind, scope_key, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_review_jobs_pending ON review_jobs(status, available_at)",
)


def ensure_tables(conn) -> None:
    """逐表建库；已存在则跳过。"""
    conn.execute(SQL_SESSIONS)
    conn.execute(SQL_SESSION_SLOTS)
    conn.execute(SQL_MESSAGES)
    conn.execute(SQL_TOOL_CALLS)
    conn.execute(SQL_REVIEW_JOBS)
    for stmt in SQL_INDEXES:
        conn.execute(stmt)
