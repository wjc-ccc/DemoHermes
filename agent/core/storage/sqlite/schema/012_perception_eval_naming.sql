-- 评测目标统一使用感知模块名；数据与指纹在同一迁移事务中更新。
CREATE TABLE eval_cases_perception (
 dataset_id TEXT NOT NULL, dataset_version TEXT NOT NULL, case_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, title TEXT NOT NULL, scenario TEXT NOT NULL, difficulty TEXT NOT NULL,
 content_hash TEXT NOT NULL, case_json TEXT NOT NULL CHECK(json_valid(case_json)),
 target TEXT NOT NULL DEFAULT 'memory' CHECK(target IN ('memory','perception','combined')),
 PRIMARY KEY(dataset_id,dataset_version,case_id),
 UNIQUE(dataset_id,dataset_version,ordinal),
 FOREIGN KEY(dataset_id,dataset_version) REFERENCES eval_datasets(dataset_id,dataset_version)
);
