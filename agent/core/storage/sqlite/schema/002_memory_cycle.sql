-- Turn-level extraction scheduling and versioned user handbook snapshots.
ALTER TABLE memory_items ADD COLUMN category TEXT;
UPDATE memory_items SET category = CASE
 WHEN type='reusable_experience' THEN 'reusable_experience'
 WHEN type='preference' THEN 'stable_preference'
 WHEN type IN ('identity','capability','relationship') THEN 'profile'
 ELSE 'ongoing_fact' END;
CREATE INDEX idx_memory_category ON memory_items(scope_key,status,category,valid_until,updated_at);

CREATE TABLE memory_review_turns (
 turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id),
 scope_key TEXT NOT NULL,
 ordinal INTEGER NOT NULL,
 batch_job_id TEXT,
 created_at INTEGER NOT NULL,
 UNIQUE(scope_key, ordinal)
);
CREATE INDEX idx_memory_review_pending ON memory_review_turns(scope_key,batch_job_id,ordinal);

CREATE TABLE memory_document_versions (
 scope_key TEXT NOT NULL,
 revision INTEGER NOT NULL,
 content TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 actor TEXT NOT NULL,
 reason TEXT NOT NULL,
 source_ref TEXT,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(scope_key,revision)
);

