-- 评测标准独立于用户记忆；发布版本不可原地覆盖，由 Store 校验指纹。
CREATE TABLE memory_eval_datasets (
 dataset_id TEXT NOT NULL, version TEXT NOT NULL, schema_version TEXT NOT NULL,
 title TEXT NOT NULL, description TEXT NOT NULL, source TEXT NOT NULL,
 case_count INTEGER NOT NULL CHECK(case_count > 0), question_count INTEGER NOT NULL CHECK(question_count >= 0),
 content_hash TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 created_at INTEGER NOT NULL,
 PRIMARY KEY(dataset_id,version)
);
CREATE TABLE memory_eval_cases (
 dataset_id TEXT NOT NULL, dataset_version TEXT NOT NULL, case_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, title TEXT NOT NULL, scenario TEXT NOT NULL, difficulty TEXT NOT NULL,
 content_hash TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 PRIMARY KEY(dataset_id,dataset_version,case_id),
 UNIQUE(dataset_id,dataset_version,ordinal),
 FOREIGN KEY(dataset_id,dataset_version) REFERENCES memory_eval_datasets(dataset_id,version)
);
CREATE INDEX idx_memory_eval_cases_scenario ON memory_eval_cases(dataset_id,dataset_version,scenario,difficulty);
CREATE TABLE memory_eval_questions (
 dataset_id TEXT NOT NULL, dataset_version TEXT NOT NULL, case_id TEXT NOT NULL, question_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, text TEXT NOT NULL, asked_at INTEGER,
 expected_action TEXT NOT NULL, requires_semantic_review INTEGER NOT NULL CHECK(requires_semantic_review IN (0,1)),
 content_hash TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 PRIMARY KEY(dataset_id,dataset_version,case_id,question_id),
 UNIQUE(dataset_id,dataset_version,case_id,ordinal),
 FOREIGN KEY(dataset_id,dataset_version,case_id) REFERENCES memory_eval_cases(dataset_id,dataset_version,case_id)
);
