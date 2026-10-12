"""评测命名迁移必须同步题库、结果引用与哈希，保留真实回答。"""
import hashlib
import json
import shutil
import sqlite3

import pytest

from agent.storage.sqlite import migrations
from agent.storage.sqlite.database import connect
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.eval.datasets import EvalDataset
from tests.eval.test_long_dialogue_contract import _definition


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _legacy_database(tmp_path, monkeypatch):
    old_schema = tmp_path / "old-schema"
    old_schema.mkdir()
    for path in migrations._SCHEMA_DIR.glob("*.sql"):
        if path.stem < "012_perception_eval_naming":
            shutil.copyfile(path, old_schema / path.name)
    database = tmp_path / "agent.db"
    with monkeypatch.context() as context:
        context.setattr(migrations, "_SCHEMA_DIR", old_schema)
        storage = RuntimeStorage.open(database)
    raw = _definition(evidence=("perception_turn",))
    raw["cases"][0]["case_id"] = "perception_case"
    raw["cases"][0]["history"][0]["turns"][0]["turn_id"] = "perception_turn"
    raw["cases"][0]["history"][0]["turns"][0]["user_text"] = "macro precision/recall/F1；decimal precision mismatch"
    raw["cases"][0]["history"][0]["turns"][1]["tool_results"][0]["result_data"] = {"precision": 0.5, "card_precision": 0.8}
    expected = EvalDataset.model_validate(raw)
    legacy = json.loads(json.dumps(expected.model_dump(mode="json")).replace("perception_", "precision_").replace('"target": "perception"', '"target": "precision"'))
    case = legacy["cases"][0]
    question = case["questions"][0]
    with storage.db.write() as connection:
        connection.execute("INSERT INTO eval_datasets VALUES (?,?,?,?,?,?,?,?,?,?,?)", (legacy["dataset_id"], legacy["version"], legacy["schema_version"], legacy["title"], legacy["description"], legacy["source"], 1, 1, _hash(legacy), json.dumps({key: value for key, value in legacy.items() if key != "cases"}), 1))
        connection.execute("INSERT INTO eval_cases VALUES (?,?,?,?,?,?,?,?,?,?)", (legacy["dataset_id"], legacy["version"], case["case_id"], 0, case["title"], case["scenario"], case["difficulty"], _hash(case), json.dumps({key: value for key, value in case.items() if key != "questions"}), case["target"]))
        connection.execute("INSERT INTO eval_questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (legacy["dataset_id"], legacy["version"], case["case_id"], question["question_id"], 0, question["text"], 1, question["expected_action"], 1, _hash(question), json.dumps(question), question["question_schema_version"], question["history_end_turn_id"]))
    return storage, expected, legacy


def test_eval_upgrade_preserves_answers_and_rebinds_every_reference(tmp_path, monkeypatch):
    storage, expected, legacy = _legacy_database(tmp_path, monkeypatch)
    storage.evals.start_run(run_id="run-1", dataset_id=legacy["dataset_id"], dataset_version="1.0.0", provider="fixture", model="fixture", prompt_version="v1", extractor_version="v1")
    actual_answer = "precision_turn 原始回答；decimal precision mismatch"
    calls = [{"response": actual_answer, "usage": {"input_tokens": 12}}]
    storage.evals.put_result("run-1", "precision_case", "q1", status="passed", result=dict(dataset_id=legacy["dataset_id"], case_id="precision_case", target="precision", actual_answer=actual_answer, model_calls=calls, question_hash=_hash(legacy["cases"][0]["questions"][0])))
    connection = connect(storage.db.path)
    try:
        migrations.migrate(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        connection.close()
    upgraded = RuntimeStorage.open(storage.db.path)
    assert upgraded.evals.get_dataset("perception_behavior", "1.0.0") == expected
    assert upgraded.evals.get_dataset("precision_behavior", "1.0.0") is None
    assert upgraded.evals.publish(expected) is False
    run = upgraded.evals.get_run("run-1")
    result = run["results"][0]
    assert run["dataset_id"] == expected.dataset_id
    assert run["details"]["naming_migration"]["original_dataset_hash"] == _hash(legacy)
    assert result["case_id"] == "perception_case"
    assert result["question_hash"] == expected.cases[0].questions[0].fingerprint()
    assert result["result"]["question_hash"] == result["question_hash"]
    assert result["result"]["dataset_id"] == expected.dataset_id
    assert result["result"]["target"] == "perception"
    assert result["result"]["actual_answer"] == actual_answer
    assert result["result"]["model_calls"] == calls
    with upgraded.db.write() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute("UPDATE eval_cases SET target='precision'")


def test_eval_upgrade_conflict_rolls_back_and_restores_foreign_keys(tmp_path, monkeypatch):
    storage, expected, legacy = _legacy_database(tmp_path, monkeypatch)
    with storage.db.write() as connection:
        connection.execute("INSERT INTO eval_datasets SELECT ?,dataset_version,schema_version,title,description,source,case_count,question_count,content_hash,dataset_json,created_at FROM eval_datasets", (expected.dataset_id,))
    connection = connect(storage.db.path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            migrations.migrate(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT target FROM eval_cases").fetchone()[0] == "precision"
        assert connection.execute("SELECT 1 FROM schema_migrations WHERE version='012_perception_eval_naming'").fetchone() is None
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='eval_cases_perception'").fetchone() is None
        assert connection.execute("SELECT dataset_id FROM eval_cases").fetchone()[0] == legacy["dataset_id"]
    finally:
        connection.close()
