-- Keep all previously applied migration files and their checksums unchanged.
ALTER TABLE sessions RENAME COLUMN payload_json TO session_json;
ALTER TABLE turns RENAME COLUMN payload_json TO turn_json;
ALTER TABLE turns RENAME COLUMN completed_at TO finished_at;
ALTER TABLE turns RENAME COLUMN error_code TO error_text;
ALTER TABLE messages RENAME COLUMN payload_json TO message_json;
ALTER TABLE tool_calls RENAME COLUMN payload_json TO tool_call_json;
ALTER TABLE context_documents DROP COLUMN revision_ref;
ALTER TABLE context_documents DROP COLUMN tokenizer_name;
UPDATE sessions SET session_json=json_remove(session_json,'$.message_count');

ALTER TABLE memory_eval_datasets RENAME TO eval_datasets;
ALTER TABLE eval_datasets RENAME COLUMN version TO dataset_version;
ALTER TABLE eval_datasets RENAME COLUMN payload_json TO dataset_json;
ALTER TABLE memory_eval_cases RENAME TO eval_cases;
ALTER TABLE eval_cases RENAME COLUMN payload_json TO case_json;
ALTER TABLE eval_cases ADD COLUMN target TEXT NOT NULL DEFAULT 'memory'
 CHECK(target IN ('memory','precision','combined'));
ALTER TABLE memory_eval_questions RENAME TO eval_questions;
ALTER TABLE eval_questions RENAME COLUMN payload_json TO question_json;
ALTER TABLE eval_questions ADD COLUMN question_schema_version TEXT NOT NULL DEFAULT '1';
ALTER TABLE eval_questions ADD COLUMN history_end_turn_id TEXT;

CREATE TABLE eval_runs (
 run_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, dataset_version TEXT NOT NULL,
 provider TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
 extractor_version TEXT NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER,
 run_json TEXT NOT NULL CHECK(json_valid(run_json)),
 FOREIGN KEY(dataset_id,dataset_version) REFERENCES eval_datasets(dataset_id,dataset_version)
);
CREATE TABLE eval_results (
 run_id TEXT NOT NULL REFERENCES eval_runs(run_id), case_id TEXT NOT NULL, question_id TEXT NOT NULL,
 question_schema_version TEXT NOT NULL, question_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('passed','failed','pending')),
 result_json TEXT NOT NULL CHECK(json_valid(result_json)),
 PRIMARY KEY(run_id,case_id,question_id)
);
CREATE INDEX idx_eval_results_status ON eval_results(run_id,status);
