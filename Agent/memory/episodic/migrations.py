"""SQLite schema migration（7.23 方案 §6.2）：PRAGMA user_version 管理版本。

v1 一次性创建 session_slots / sessions / messages / tool_calls / review_jobs；
后续演进（FTS5、environment_snapshots）以独立 migration 追加，禁止改旧版本。
"""

SCHEMA_V1 = """
CREATE TABLE sessions (
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
);

CREATE TABLE session_slots (
  session_key       TEXT PRIMARY KEY,
  active_session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
  scope_kind        TEXT NOT NULL CHECK(scope_kind IN ('personal', 'group')),
  scope_key         TEXT NOT NULL,
  updated_at        REAL NOT NULL
);

CREATE TABLE messages (
  message_id      TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
  seq_id          INTEGER NOT NULL,
  role            TEXT NOT NULL,
  content_text    TEXT NOT NULL,
  created_at      REAL NOT NULL,
  payload_json    TEXT NOT NULL,
  schema_version  INTEGER NOT NULL DEFAULT 1,
  UNIQUE(session_id, seq_id)
);

CREATE TABLE tool_calls (
  tool_call_id    TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
  trajectory_id   TEXT NOT NULL,
  tool_name       TEXT NOT NULL,
  status          TEXT NOT NULL,
  created_at      REAL NOT NULL,
  payload_json    TEXT NOT NULL,
  schema_version  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE review_jobs (
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
);

CREATE INDEX idx_sessions_scope ON sessions(scope_kind, scope_key, updated_at);
CREATE INDEX idx_messages_session ON messages(session_id, seq_id);
CREATE INDEX idx_messages_created ON messages(created_at);
CREATE INDEX idx_tool_calls_session ON tool_calls(session_id, created_at);
CREATE INDEX idx_review_jobs_pending ON review_jobs(status, available_at);
"""

SCHEMA_VERSION = 1


def migrate(conn) -> None:
    """把数据库迁移到最新版本；只前进，不回退。"""
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA_V1)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
