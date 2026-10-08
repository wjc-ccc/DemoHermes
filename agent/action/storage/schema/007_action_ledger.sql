CREATE TABLE action_items (
 entry_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
 scope_key TEXT NOT NULL, tool_name TEXT NOT NULL,
 effect_state TEXT NOT NULL CHECK(effect_state IN ('not_started','started','succeeded','failed','unknown')),
 phase TEXT NOT NULL, publish_state TEXT NOT NULL,
 revision INTEGER NOT NULL, resource_ref TEXT, lease_expires_at INTEGER,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
CREATE INDEX idx_action_scope ON action_items(scope_key,updated_at);
CREATE INDEX idx_action_recovery ON action_items(effect_state,lease_expires_at);
CREATE TABLE action_attempts (
 attempt_id TEXT PRIMARY KEY, entry_id TEXT REFERENCES action_items(entry_id),
 tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, scope_key TEXT NOT NULL,
 turn_id TEXT NOT NULL, admitted INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL, created_at INTEGER NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
CREATE INDEX idx_action_budget ON action_attempts(scope_key,turn_id,tool_name,admitted);
CREATE TABLE action_events (
 event_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES action_items(entry_id),
 revision INTEGER NOT NULL, event_type TEXT NOT NULL, created_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
CREATE TABLE action_resource_claims (
 scope_key TEXT NOT NULL, resource_ref TEXT NOT NULL,
 entry_id TEXT NOT NULL REFERENCES action_items(entry_id), lease_token TEXT NOT NULL,
 PRIMARY KEY(scope_key,resource_ref)
);
CREATE TABLE action_observations (
 observation_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, turn_id TEXT NOT NULL,
 step_id TEXT, resource_ref TEXT NOT NULL, resource_version TEXT NOT NULL,
 attempt_id TEXT NOT NULL REFERENCES action_attempts(attempt_id), created_at INTEGER NOT NULL
);
CREATE INDEX idx_action_read ON action_observations(scope_key,resource_ref,resource_version,turn_id,step_id);
CREATE TABLE action_approvals (
 approval_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, tool_name TEXT NOT NULL,
 binding_hash TEXT NOT NULL, actor_id TEXT NOT NULL, expires_at INTEGER NOT NULL,
 created_at INTEGER NOT NULL
);
CREATE TABLE action_contracts (
 scope_key TEXT NOT NULL, turn_id TEXT NOT NULL, tool_name TEXT NOT NULL,
 contract_hash TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 PRIMARY KEY(scope_key,turn_id,tool_name)
);
