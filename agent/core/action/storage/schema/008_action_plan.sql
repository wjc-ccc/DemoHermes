CREATE TABLE action_plans (
 plan_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, revision INTEGER NOT NULL,
 status TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
CREATE TABLE action_steps (
 plan_id TEXT NOT NULL REFERENCES action_plans(plan_id), step_id TEXT NOT NULL,
 status TEXT NOT NULL, lease_token TEXT, lease_expires_at INTEGER,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), PRIMARY KEY(plan_id,step_id)
);
CREATE TABLE action_patches (
 patch_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES action_plans(plan_id),
 base_revision INTEGER NOT NULL, created_at INTEGER NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
);
ALTER TABLE action_observations ADD COLUMN plan_id TEXT;
