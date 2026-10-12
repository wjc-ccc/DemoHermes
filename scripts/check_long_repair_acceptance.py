"""Evaluate the agreed gates from immutable questions and saved real evidence."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.eval.datasets.long_dialogues import load_long_datasets
from agent.eval.long_dialogue import _implementation_hash
from scripts.summarize_long_repair import summarize


def check(root):
    root = Path(root).resolve()
    definitions = load_long_datasets()
    questions = {(case.case_id, question.question_id): (case, question)
                 for dataset in definitions for case in dataset.cases for question in case.questions}
    pairs_expected = {identity for identity, (case, _) in questions.items() if case.target in {"perception", "combined"}}
    results = [json.loads(path.read_text(encoding="utf-8")) for path in root.glob("*__q*/result.json")]
    actual = {(result["case_id"], result["question_id"]) for result in results}
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    summarize(root)
    quality = json.loads((root / "quality-comparison.json").read_text(encoding="utf-8"))
    audit_path = root / "storage-audit.json"
    storage = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
    attempts = [attempt for result in results for attempt in [result, *result.get("reruns", [])]]
    pairs = quality["actual_input_comparisons"]
    negative = [result for result in results if not questions[(result["case_id"], result["question_id"])][0].expected_memories
                and questions[(result["case_id"], result["question_id"])][0].target != "perception"]
    gates = dict(
        all_96_questions=actual == set(questions) and len(results) == 96,
        frozen_versions=manifest["dataset_hashes"] == {dataset.dataset_id: dataset.fingerprint() for dataset in definitions},
        current_implementation=manifest["implementation_hash"] == _implementation_hash()
            and all(attempt.get("implementation_hash") == manifest["implementation_hash"] for attempt in attempts),
        question_fingerprints=all(result.get("question_hash") == questions[(result["case_id"], result["question_id"])][1].fingerprint() for result in results),
        storage_all_passed=storage.get("questions") == 96 and storage.get("passed") == 96,
        hard_checks_all_passed=bool(attempts) and all(attempt.get("hard_checks") and all(attempt["hard_checks"].values()) for attempt in attempts),
        pure_negative_zero_cards=bool(negative) and all(not attempt.get("cards") for result in negative for attempt in [result, *result.get("reruns", [])]),
        forbidden_and_violations_zero=bool(attempts) and all(attempt.get("semantic_review") is not None
            and not attempt["semantic_review"].get("forbidden_card_ids") and not attempt["semantic_review"].get("violations") for attempt in attempts),
        semantic_reviews_complete=quality["semantic_reviews"] == 96,
        semantic_accuracy_at_least_90=quality["semantic_accuracy"] is not None and quality["semantic_accuracy"] >= .90,
        memory_recall_at_least_90=quality["memory_recall"] is not None and quality["memory_recall"] >= .90,
        all_48_input_comparisons={(pair["case_id"], pair["question_id"]) for pair in pairs} == pairs_expected and len(pairs) == 48,
        input_saving_at_least_30=bool(pairs) and all(pair["saving"] >= .30 for pair in pairs),
        whole_turn_input_saving_at_least_30=bool(pairs) and all(pair["total_saving"] is not None and pair["total_saving"] >= .30 for pair in pairs),
        failed_questions_repeated_twice=all(len(result.get("reruns", [])) == 2 for result in results if not result["passed"]),
    )
    report = dict(accepted=all(gates.values()), gates=gates,
                  thresholds=dict(semantic_accuracy=.90, memory_recall=.90, main_input_saving=.30),
                  failed_gates=[name for name, passed in gates.items() if not passed],
                  metrics={name: quality[name] for name in ("questions", "semantic_reviews", "semantic_correct", "semantic_accuracy",
                                                            "expected_memories", "matched_memories", "memory_recall")},
                  minimum_actual_input_saving=min((pair["saving"] for pair in pairs), default=None),
                  minimum_whole_turn_input_saving=min((pair["total_saving"] for pair in pairs if pair["total_saving"] is not None), default=None),
                  implementation_hash=manifest["implementation_hash"],
                  limitations="JEV服务排除；Python和前端回归的独立执行证据另见验收台账。")
    (root / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    report = check(args.root)
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["accepted"] else 1)
