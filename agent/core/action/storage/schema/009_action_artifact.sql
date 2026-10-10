CREATE TABLE action_artifacts (
 artifact_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, plan_id TEXT NOT NULL,
 step_id TEXT NOT NULL, segment_id TEXT NOT NULL, status TEXT NOT NULL,
 content_hash TEXT NOT NULL, input_hash TEXT NOT NULL, created_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
CREATE INDEX idx_action_segment ON action_artifacts(plan_id,step_id,segment_id,created_at);
CREATE TABLE action_gate_decisions (
 decision_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES action_artifacts(artifact_id),
 gate_fingerprint TEXT NOT NULL, verdict TEXT NOT NULL, created_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
CREATE TABLE action_turns (
 scope_key TEXT NOT NULL, turn_id TEXT NOT NULL,
 context_json TEXT NOT NULL CHECK(json_valid(context_json)), PRIMARY KEY(scope_key,turn_id)
);
