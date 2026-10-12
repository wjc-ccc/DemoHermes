"""独立核验长对话评测覆盖、版本、未来信息隔离和未返回调用成本。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.eval.core_benchmark import evaluation_fingerprint, production_fingerprints, reuse_definitions
from agent.eval.datasets.core_benchmark import load_definitions
from agent.types.perception.trace import normalized_usage


def update_fact_retrieval(bucket, row, question):
    """事实只计一次；多个匹配卡中任意一张命中就具备读回该事实的机会。"""
    expected = len(question.expected_active_memory_ids or ())
    bucket["checkpoints"] += 1
    bucket["expected_facts"] += expected
    if row.get("grade_status") != "completed":
        bucket["unscored_expected_facts"] += expected
        bucket["unscored_checkpoints"] += 1
        return
    matched = set(row.get("review", {}).get("matched_memory_ids", []))
    pairs = defaultdict(set)
    for match in row.get("review", {}).get("memory_matches", []):
        if match["memory_id"] in matched:
            pairs[match["memory_id"]].update(match["card_ids"])
    refs = row.get("retrieval_ids", [])
    bucket["graded_expected_facts"] += expected
    bucket["matched_facts"] += len(matched)
    bucket["hit_at_5"] += sum(bool(ids & set(refs[:5])) for ids in pairs.values())
    bucket["hit_at_10"] += sum(bool(ids & set(refs[:10])) for ids in pairs.values())


def audit(root: Path) -> dict:
    root = root.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    definitions = load_definitions(pilot=manifest["pilot"])
    reuse = reuse_definitions(definitions) if manifest["include_reuse"] else ()
    lookup = {(d.dataset_id, c.case_id, q.question_id): (c, q)
              for d in (*definitions, *reuse) for c in d.cases for q in c.questions}
    expected = set()
    for definition in (*definitions, *reuse):
        conditions = ("P0M1", "P1M1") if definition in reuse else ("P0M0", "P0M1", "P1M0", "P1M1")
        expected.update((definition.dataset_id, c.case_id, q.question_id, r, condition)
                        for c in definition.cases for q in c.questions
                        for r in range(1, manifest["config"]["repeats"] + 1) for condition in conditions)
    actual = Counter()
    grades = Counter()
    hard_failures = Counter()
    source_cutoff_failures = []
    question_hash_failures = []
    paired_sources = defaultdict(set)
    fact_retrieval = defaultdict(Counter)
    row_call_ids = set()
    for folder in ("cases", "reuse"):
        for path in (root / folder).glob("**/result.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            key = tuple(row[k] for k in ("dataset_id", "case_id", "question_id", "repeat", "condition"))
            actual[key] += 1
            grades[row.get("grade_status", "missing")] += 1
            hard_failures.update(k for k, ok in row.get("hard_checks", {}).items() if not ok)
            case, question = lookup[key[:3]]
            if folder == "cases" and row["target"] != "perception" and row["condition"].endswith("M1"):
                update_fact_retrieval(fact_retrieval[row["condition"]], row, question)
            if row["question_hash"] != question.fingerprint():
                question_hash_failures.append(list(key))
            allowed_turns = {t.turn_id for s in case.history_for(question) for t in s.turns}
            allowed_tools = {tool.tool_call_id for s in case.history_for(question) for t in s.turns for tool in t.tool_results}
            source = row["source"]
            if not set(source["source_map"]) <= allowed_turns or not set(source["tool_map"]) <= allowed_tools:
                source_cutoff_failures.append(list(key))
            if folder == "cases":
                digest = hashlib.sha256(json.dumps([source["source_map"], source["tool_map"], source["session_id"], source["previous_session_ids"]], sort_keys=True).encode()).hexdigest()
                paired_sources[key[:4] + (row["condition"][-2:],)].add(digest)
            for call in row.get("model_calls", []) + source.get("source_calls", []):
                if call.get("call_id"):
                    row_call_ids.add(call["call_id"])
    # 原始日志只遍历一次，按调用 ID 去重；未返回 usage 的在途记录不会消失。
    calls = {}
    malformed_logs = []
    for path in root.glob("**/*calls.jsonl"):
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    malformed_logs.append(dict(path=str(path.relative_to(root)), line=index + 1))
                    continue
                identity = value.get("call_id", f"{path.relative_to(root)}:{index}")
                a, b = normalized_usage(value.get("usage") or {})
                calls[identity] = dict(purpose=value.get("purpose", "unknown"), input=a, output=b,
                                       failed=bool(value.get("error")), duration_ms=value.get("duration_ms"), path=str(path.relative_to(root)))
    completed_call_ids = set(calls)
    pending = []
    for path in root.glob("**/inflight/*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        identity = value["call_id"]
        if identity in calls:
            continue
        relative = path.relative_to(root)
        pending.append(dict(call_id=identity, purpose=value["purpose"], path=str(relative), started_at=value["started_at"]))
        calls[identity] = dict(purpose=value["purpose"], input=None, output=None, failed=None, duration_ms=None, path=str(relative))
    totals = {}
    for purpose in sorted({c["purpose"] for c in calls.values()}):
        values = [c for c in calls.values() if c["purpose"] == purpose]
        missing_a = sum(c["input"] is None for c in values)
        missing_b = sum(c["output"] is None for c in values)
        known_a = sum(c["input"] for c in values if c["input"] is not None)
        known_b = sum(c["output"] for c in values if c["output"] is not None)
        totals[purpose] = dict(calls=len(values), input_tokens=None if missing_a else known_a,
                               output_tokens=None if missing_b else known_b, known_input_tokens=known_a,
                               known_output_tokens=known_b, missing_input_usage=missing_a, missing_output_usage=missing_b,
                               duration_ms=None if any(c["duration_ms"] is None for c in values) else sum(c["duration_ms"] for c in values),
                               known_duration_ms=sum(c["duration_ms"] for c in values if c["duration_ms"] is not None),
                               failed_calls=sum(bool(c["failed"]) for c in values))
    database = root / "evaluations.db"
    db = {}
    if database.exists():
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
            db = dict(foreign_key_errors=conn.execute("PRAGMA foreign_key_check").fetchall(),
                      datasets=conn.execute("SELECT dataset_id,dataset_version,schema_version,question_count,content_hash FROM eval_datasets ORDER BY dataset_id").fetchall(),
                      run_count=conn.execute("SELECT count(*) FROM eval_runs").fetchone()[0],
                      result_count=conn.execute("SELECT count(*) FROM eval_results").fetchone()[0],
                      result_statuses=dict(conn.execute("SELECT status,count(*) FROM eval_results GROUP BY status")),
                      question_hash_mismatches=conn.execute("SELECT count(*) FROM eval_results e JOIN eval_runs r ON r.run_id=e.run_id JOIN eval_questions q ON q.dataset_id=r.dataset_id AND q.dataset_version=r.dataset_version AND q.case_id=e.case_id AND q.question_id=e.question_id WHERE e.question_hash<>q.content_hash").fetchone()[0])
    result = dict(
        audit_version="core-benchmark-integrity-v1", audit_code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        production_unchanged=manifest["production"] == production_fingerprints(),
        evaluation_unchanged=manifest["evaluation_hash"] == evaluation_fingerprint(),
        dataset_unchanged=manifest["dataset_hashes"] == {d.dataset_id: d.fingerprint() for d in definitions},
        planned_answers=len(expected), actual_answers=sum(actual.values()),
        missing_answer_keys=[list(k) for k in sorted(expected - actual.keys())],
        unexpected_answer_keys=[list(k) for k in sorted(actual.keys() - expected)],
        duplicate_answer_keys=[list(k) for k, count in actual.items() if count != 1],
        grades=dict(grades), hard_check_failures=dict(hard_failures), question_hash_failures=question_hash_failures,
        source_cutoff_failures=source_cutoff_failures,
        perception_pair_source_mismatches=[list(k) for k, hashes in paired_sources.items() if len(hashes) != 1],
        actual_unique_calls=len(calls), actual_unique_call_totals_by_stage=totals,
        interrupted_usage_unknown=pending, malformed_logs=malformed_logs,
        completed_product_calls_without_row_attribution_records=[dict(call_id=k, **c) for k, c in calls.items() if c["purpose"] != "review" and k not in row_call_ids and k in completed_call_ids],
        completed_product_calls_without_row_attribution=sum(c["purpose"] != "review" and k not in row_call_ids and k in completed_call_ids for k, c in calls.items()),
        database=db,
        cost_caveat="未知调用不能按零计费；原报表的已返回调用成本只可作为完整度核对前的已知量。",
    )
    result["memory_fact_retrieval"] = {}
    for condition, bucket in fact_retrieval.items():
        ratio = lambda a, b: a / b if b else None
        result["memory_fact_retrieval"][condition] = dict(
            **bucket, confirmed_missed_facts=bucket["graded_expected_facts"] - bucket["matched_facts"],
            fact_recall_at_5=ratio(bucket["hit_at_5"], bucket["expected_facts"]),
            fact_recall_at_10=ratio(bucket["hit_at_10"], bucket["expected_facts"]),
            conditional_fact_recall_at_5=ratio(bucket["hit_at_5"], bucket["matched_facts"]),
            conditional_fact_recall_at_10=ratio(bucket["hit_at_10"], bucket["matched_facts"]),
            method="补充统计按黄金事实计数，命中任一匹配卡即召回；未知评分不计命中。原卡片 Recall 与严格全卡命中指标保留。",
        )
    result["complete_answer_coverage"] = actual.keys() == expected and all(n == 1 for n in actual.values())
    result["full_product_cost_known"] = not malformed_logs and all(c["input"] is not None and c["output"] is not None for c in calls.values() if c["purpose"] != "review")
    (root / "integrity-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    result = audit(parser.parse_args().root)
    print(json.dumps({k: result[k] for k in ("actual_answers", "planned_answers", "grades", "production_unchanged", "evaluation_unchanged", "full_product_cost_known")}, ensure_ascii=False))
