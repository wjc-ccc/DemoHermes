"""一次性迁移评测模块标识，重新绑定内容指纹并保留真实回答。"""
from __future__ import annotations

import json
import re

from agent.types.eval.datasets import EvalDataset


def _name(value):
    protected = []
    def keep(match):
        protected.append(match.group())
        return f"@@METRIC{len(protected)-1}@@"
    value = re.sub(r"(?:matched_gold_)?card_precision|extraction_precision|decimal precision mismatch|macro precision/recall/F1|(?:卡片(?:识别)?|card|Card)[ ]*Precision|Recall[ /]*Precision", keep, value)
    value = re.sub(r"percision|precision", lambda match: "PERCEPTION" if match.group().isupper()
                   else "Perception" if match.group()[0].isupper() else "perception", value)
    for index, original in enumerate(protected):
        value = value.replace(f"@@METRIC{index}@@", original)
    return value


def _definition(value):
    if isinstance(value, dict):
        return {key if key == "precision" else _name(key): _definition(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_definition(item) for item in value]
    if isinstance(value, str):
        return _name(value)
    return value


def _result_metadata(value):
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for field in ("dataset_id", "case_id", "target"):
        if isinstance(result.get(field), str):
            result[field] = _name(result[field])
    return result


def migrate_eval_names(conn):
    datasets = conn.execute("SELECT dataset_id,dataset_version,dataset_json,content_hash FROM eval_datasets").fetchall()
    for dataset_id, version, header, original_hash in datasets:
        payload = json.loads(header)
        payload["cases"] = []
        cases = conn.execute("SELECT case_id,case_json,ordinal FROM eval_cases WHERE dataset_id=? AND dataset_version=? ORDER BY ordinal", (dataset_id, version)).fetchall()
        original_cases = []
        for case_id, case_json, ordinal in cases:
            case = json.loads(case_json)
            questions = conn.execute("SELECT question_id,question_json,ordinal FROM eval_questions WHERE dataset_id=? AND dataset_version=? AND case_id=? ORDER BY ordinal", (dataset_id, version, case_id)).fetchall()
            case["questions"] = [json.loads(row[1]) for row in questions]
            payload["cases"].append(case)
            original_cases.append((case_id, ordinal, questions))
        definition = EvalDataset.model_validate(_definition(payload))
        conn.execute("UPDATE eval_datasets SET dataset_id=?,title=?,description=?,content_hash=?,dataset_json=? WHERE dataset_id=? AND dataset_version=?", (definition.dataset_id, definition.title, definition.description, definition.fingerprint(), definition.model_dump_json(exclude={"cases"}), dataset_id, version))
        for case, (old_case_id, ordinal, questions) in zip(definition.cases, original_cases):
            conn.execute("INSERT INTO eval_cases_perception VALUES (?,?,?,?,?,?,?,?,?,?)", (definition.dataset_id, version, case.case_id, ordinal, case.title, case.scenario, case.difficulty, case.fingerprint(), case.model_dump_json(exclude={"questions"}), case.target))
            for question, (old_question_id, _, _) in zip(case.questions, questions):
                conn.execute("UPDATE eval_questions SET dataset_id=?,case_id=?,question_id=?,text=?,content_hash=?,question_json=?,history_end_turn_id=? WHERE dataset_id=? AND dataset_version=? AND case_id=? AND question_id=?", (definition.dataset_id, case.case_id, question.question_id, question.text, question.fingerprint(), question.model_dump_json(), question.history_end_turn_id, dataset_id, version, old_case_id, old_question_id))
                rows = conn.execute("SELECT e.run_id,e.result_json FROM eval_results e JOIN eval_runs r ON r.run_id=e.run_id WHERE r.dataset_id=? AND r.dataset_version=? AND e.case_id=? AND e.question_id=?", (dataset_id, version, old_case_id, old_question_id)).fetchall()
                for run_id, result_json in rows:
                    result = _result_metadata(json.loads(result_json))
                    if "question_hash" in result:
                        result["question_hash"] = question.fingerprint()
                    conn.execute("UPDATE eval_results SET case_id=?,question_id=?,question_hash=?,result_json=? WHERE run_id=? AND case_id=? AND question_id=?", (case.case_id, question.question_id, question.fingerprint(), json.dumps(result, ensure_ascii=False, separators=(",", ":")), run_id, old_case_id, old_question_id))
        runs = conn.execute("SELECT run_id,run_json FROM eval_runs WHERE dataset_id=? AND dataset_version=?", (dataset_id, version)).fetchall()
        for run_id, run_json in runs:
            details = _result_metadata(json.loads(run_json))
            if original_hash != definition.fingerprint():
                details["naming_migration"] = dict(original_dataset_hash=original_hash, dataset_hash=definition.fingerprint())
            conn.execute("UPDATE eval_runs SET dataset_id=?,run_json=? WHERE run_id=?", (definition.dataset_id, json.dumps(details, ensure_ascii=False, separators=(",", ":")), run_id))
    conn.execute("DROP TABLE eval_cases")
    conn.execute("ALTER TABLE eval_cases_perception RENAME TO eval_cases")
    conn.execute("CREATE INDEX idx_memory_eval_cases_scenario ON eval_cases(dataset_id,dataset_version,scenario,difficulty)")
