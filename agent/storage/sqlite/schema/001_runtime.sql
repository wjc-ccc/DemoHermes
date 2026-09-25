-- 所有 SQL 时间列均为 UTC Unix 毫秒；领域载荷保持原有时间精度和 Anchor 结构。
CREATE TABLE sessions (
 session_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
 is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),
 scope_kind TEXT NOT NULL, scope_key TEXT NOT NULL,
 organization_id TEXT, tenant_id TEXT, user_id TEXT, project_id TEXT,
 status TEXT NOT NULL, channel TEXT NOT NULL, author_id TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, last_active_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
CREATE UNIQUE INDEX uq_sessions_current ON sessions(session_key) WHERE is_current=1;
CREATE INDEX idx_sessions_scope ON sessions(scope_key,last_active_at);
CREATE TABLE turns (
 turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
 sequence_no INTEGER NOT NULL, status TEXT NOT NULL,
 started_at INTEGER NOT NULL, completed_at INTEGER NOT NULL,
 input_token_count INTEGER, output_token_count INTEGER, error_code TEXT,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 environment_json TEXT CHECK(environment_json IS NULL OR json_valid(environment_json)),
 created_at INTEGER NOT NULL, UNIQUE(session_id,sequence_no)
);
CREATE TABLE messages (
 message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
 turn_id TEXT REFERENCES turns(turn_id), seq_id INTEGER NOT NULL,
 role TEXT NOT NULL, content_text TEXT NOT NULL, source_type TEXT NOT NULL,
 created_at INTEGER NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 UNIQUE(session_id,seq_id)
);
CREATE INDEX idx_messages_turn ON messages(turn_id,seq_id);
CREATE TABLE tool_calls (
 tool_call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
 turn_id TEXT REFERENCES turns(turn_id), trajectory_id TEXT NOT NULL,
 tool_name TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL,
 workspace_item_id TEXT, provenance_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(provenance_json)),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
CREATE INDEX idx_tools_turn ON tool_calls(turn_id,created_at);
CREATE TABLE session_workspaces (
 session_id TEXT PRIMARY KEY REFERENCES sessions(session_id), goal_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL CHECK(status IN ('active','paused','completed','cancelled')),
 revision INTEGER NOT NULL, plan_revision INTEGER NOT NULL,
 contract_revision INTEGER NOT NULL DEFAULT 0, current_item_id TEXT, pause_reason TEXT,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE workspace_items (
 item_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES session_workspaces(session_id),
 status TEXT NOT NULL CHECK(status IN ('blocked','ready','running','completed','failed','cancelled')),
 revision INTEGER NOT NULL, executor_ref TEXT NOT NULL,
 dependencies_json TEXT NOT NULL CHECK(json_valid(dependencies_json)),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 PRIMARY KEY(session_id,item_id)
);
CREATE INDEX idx_workspace_status ON workspace_items(session_id,status);
CREATE TABLE goal_contract_versions (
 session_id TEXT NOT NULL REFERENCES session_workspaces(session_id), revision INTEGER NOT NULL,
 goal_id TEXT NOT NULL, source_message_id TEXT NOT NULL REFERENCES messages(message_id),
 changed_by TEXT NOT NULL, change_reason TEXT NOT NULL, created_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), PRIMARY KEY(session_id,revision)
);
CREATE TABLE narrative_events (
 item_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
 sequence_no INTEGER NOT NULL, event_type TEXT NOT NULL, workspace_item_id TEXT,
 author TEXT NOT NULL, created_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), UNIQUE(session_id,sequence_no)
);
CREATE TABLE session_state_items (
 item_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
 scope_key TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
 validity TEXT NOT NULL CHECK(validity IN ('valid','invalidated')),
 value_type TEXT NOT NULL, value_json TEXT NOT NULL CHECK(json_valid(value_json)),
 source_type TEXT NOT NULL, source_id TEXT NOT NULL, workspace_item_id TEXT,
 created_at INTEGER NOT NULL, created_by TEXT NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 UNIQUE(session_id,scope_key,key,revision)
);
CREATE INDEX idx_state_current ON session_state_items(session_id,scope_key,key,revision DESC);
CREATE TABLE memory_items (
 id TEXT PRIMARY KEY, layer TEXT NOT NULL CHECK(layer='user'), type TEXT NOT NULL, memory_kind TEXT,
 organization_id TEXT, tenant_id TEXT, user_id TEXT, project_id TEXT, scope_key TEXT NOT NULL,
 normalized_key TEXT NOT NULL, title TEXT, summary TEXT NOT NULL,
 content_json TEXT NOT NULL CHECK(json_valid(content_json)),
 keywords_json TEXT NOT NULL CHECK(json_valid(keywords_json)),
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
 status TEXT NOT NULL CHECK(status IN ('proposed','active','superseded','rejected','expired','deleted')),
 priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
 confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), source_authority TEXT NOT NULL,
 valid_from INTEGER, valid_until INTEGER, supersedes_item_id TEXT REFERENCES memory_items(id),
 revision INTEGER NOT NULL, content_hash TEXT NOT NULL, token_count INTEGER NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, last_confirmed_at INTEGER
);
CREATE INDEX idx_memory_scope ON memory_items(scope_key,status,normalized_key);
CREATE TABLE memory_item_versions (
 memory_item_id TEXT NOT NULL REFERENCES memory_items(id), revision INTEGER NOT NULL,
 snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)), changed_at INTEGER NOT NULL,
 PRIMARY KEY(memory_item_id,revision)
);
CREATE TABLE context_documents (
 id TEXT PRIMARY KEY, layer TEXT NOT NULL, type TEXT NOT NULL, scope_key TEXT NOT NULL,
 organization_id TEXT, tenant_id TEXT, user_id TEXT, project_id TEXT,
 path TEXT NOT NULL, content_hash TEXT NOT NULL, token_count INTEGER NOT NULL,
 tokenizer_name TEXT NOT NULL, revision_ref TEXT, last_loaded_at INTEGER,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(layer,path,scope_key)
);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, job_type TEXT NOT NULL, handler_version TEXT NOT NULL,
 session_id TEXT REFERENCES sessions(session_id), turn_id TEXT REFERENCES turns(turn_id),
 dedupe_key TEXT NOT NULL UNIQUE, input_hash TEXT NOT NULL,
 input_json TEXT NOT NULL CHECK(json_valid(input_json)),
 result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
 status TEXT NOT NULL CHECK(status IN ('pending','running','completed','retryable_failed','terminal_failed','cancelled')),
 revision INTEGER NOT NULL DEFAULT 1, attempt_count INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_token TEXT, lease_expires_at INTEGER,
 created_at INTEGER NOT NULL, available_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
 last_error TEXT
);
CREATE INDEX idx_jobs_ready ON jobs(job_type,status,available_at);
CREATE TABLE traces (
 id TEXT PRIMARY KEY, module TEXT NOT NULL, operation TEXT NOT NULL,
 session_id TEXT, turn_id TEXT, job_id TEXT, created_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
CREATE INDEX idx_traces_module ON traces(module,created_at);
CREATE INDEX idx_traces_session ON traces(session_id,created_at);
CREATE TABLE trace_items (
 id TEXT PRIMARY KEY, trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
 item_id TEXT, decision TEXT NOT NULL, created_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 ai_json TEXT NOT NULL CHECK(json_valid(ai_json)), runtime_json TEXT NOT NULL CHECK(json_valid(runtime_json))
);
CREATE INDEX idx_trace_items_parent ON trace_items(trace_id);
CREATE INDEX idx_trace_items_item ON trace_items(item_id);
CREATE TABLE audit_events (
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, operation TEXT NOT NULL, target TEXT NOT NULL,
 turn_id TEXT, scope_key TEXT NOT NULL, created_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
