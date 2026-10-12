"""验证正式阶段完成后的恢复不重新回答，并导入复用题库版本。"""
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent.eval.datasets.core_benchmark import load_definitions
from agent.types.eval.core_benchmark import BenchmarkConfig
from scripts import resume_core_benchmark as driver
from scripts import audit_core_benchmark as auditor


def setup_run(tmp_path, monkeypatch, *, complete=True):
    original = load_definitions(pilot=True)[0]
    case = original.cases[0].model_copy(update={"questions": original.cases[0].questions[:1]})
    definition = original.model_copy(update={"cases": (case,)})
    reuse = definition.model_copy(update={"dataset_id": "perception_core_reuse"})
    config = BenchmarkConfig(repeats=1, workers=1)
    manifest = dict(pilot=False, case_ids=None, config=asdict(config),
                    dataset_hashes={definition.dataset_id: definition.fingerprint()},
                    provider="fixture", model="fixture", endpoint_hash=hashlib.sha256(b"").hexdigest(),
                    include_reuse=True, production={}, evaluation_hash="frozen")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if complete:
        for condition in ("P0M0", "P0M1", "P1M0", "P1M1"):
            directory = tmp_path / "cases" / condition
            directory.mkdir(parents=True)
            row = dict(dataset_id=definition.dataset_id, case_id=case.case_id,
                       question_id=case.questions[0].question_id, repeat=1, condition=condition, target=case.target)
            (directory / "result.json").write_text(json.dumps(row), encoding="utf-8")
    calls = []
    monkeypatch.setattr(driver, "check_run", lambda root: None)
    monkeypatch.setattr(driver, "load_definitions", lambda **kwargs: (definition,))
    monkeypatch.setattr(driver, "reuse_definitions", lambda definitions: (reuse,))
    delegate = SimpleNamespace(name="fixture", model_name="fixture", base_url="")
    monkeypatch.setattr(driver, "ModelRegistry", SimpleNamespace(default=lambda: SimpleNamespace(get=lambda: delegate)))
    monkeypatch.setattr(driver, "run_reuse_case", lambda *args: calls.append(("reuse", args[0].dataset_id)))
    monkeypatch.setattr(driver, "grade_results", lambda root, definitions, *args: calls.append(("grade", [d.dataset_id for d in definitions])))
    monkeypatch.setattr(driver, "import_results", lambda root, definitions, *args: calls.append(("import", [d.dataset_id for d in definitions])))
    monkeypatch.setattr(driver, "build_report", lambda root: calls.append(("report",)))
    monkeypatch.setattr(driver, "production_fingerprints", lambda: {})
    return calls, definition, reuse, case


def test_stage_resume_preserves_formal_answers_and_imports_reuse_versions(tmp_path, monkeypatch):
    calls, definition, reuse, case = setup_run(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in (tmp_path / "cases").glob("**/result.json")}
    driver.resume(tmp_path)
    assert calls[0] == ("reuse", reuse.dataset_id)
    assert ("import", [definition.dataset_id, reuse.dataset_id]) in calls
    assert all(path.read_bytes() == content for path, content in before.items())
    saved = json.loads((tmp_path / "stage-resume-manifest.json").read_text(encoding="utf-8"))
    assert saved["formal_answers_reused"] == 4
    assert len(saved["driver_hash"]) == 64
    assert json.loads((tmp_path / "completed.json").read_text(encoding="utf-8"))["production_unchanged"]


def test_stage_resume_skips_completed_reuse_and_refuses_incomplete_formal(tmp_path, monkeypatch):
    calls, definition, reuse, case = setup_run(tmp_path, monkeypatch)
    for condition in ("P0M1", "P1M1"):
        directory = tmp_path / "reuse" / case.case_id / "repeat-1" / condition
        directory.mkdir(parents=True)
        (directory / "sequence.json").write_text("{}", encoding="utf-8")
    driver.resume(tmp_path)
    assert not any(call[0] == "reuse" for call in calls)
    next((tmp_path / "cases").glob("**/result.json")).unlink()
    calls.clear()
    with pytest.raises(ValueError, match="正式回答尚未完整"):
        driver.resume(tmp_path)
    assert calls == []


def test_audit_counts_unfinished_usage_and_does_not_duplicate_completed_call(tmp_path, monkeypatch):
    calls, definition, reuse, case = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr(auditor, "load_definitions", lambda **kwargs: (definition,))
    monkeypatch.setattr(auditor, "reuse_definitions", lambda definitions: ())
    monkeypatch.setattr(auditor, "production_fingerprints", lambda: {})
    monkeypatch.setattr(auditor, "evaluation_fingerprint", lambda: "frozen")
    for path in (tmp_path / "cases").glob("**/result.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        row.update(question_hash=case.questions[0].fingerprint(), grade_status="completed", hard_checks={"scope_isolated": True},
                   source=dict(source_map={}, tool_map={}, session_id="shared", previous_session_ids=[], source_calls=[]))
        path.write_text(json.dumps(row), encoding="utf-8")
    log = tmp_path / "model-calls.jsonl"
    logged = dict(call_id="done", purpose="anchor", usage={"prompt_tokens":13, "completion_tokens":4})
    log.write_text(json.dumps(logged) + "\n", encoding="utf-8")
    pending = tmp_path / "inflight"
    pending.mkdir()
    for identity in ("done", "interrupted"):
        (pending / f"{identity}.json").write_text(json.dumps(dict(call_id=identity, purpose="anchor", started_at=1)), encoding="utf-8")
    result = auditor.audit(tmp_path)
    assert result["complete_answer_coverage"]
    assert result["actual_unique_calls"] == 2
    assert len(result["interrupted_usage_unknown"]) == 1
    usage = result["actual_unique_call_totals_by_stage"]["anchor"]
    assert usage["input_tokens"] is None
    assert usage["known_input_tokens"] == 13
    assert not result["full_product_cost_known"]


def test_fact_retrieval_accepts_any_matching_card_and_keeps_unscored_separate():
    question = SimpleNamespace(expected_active_memory_ids=("profile",))
    row = dict(grade_status="completed", retrieval_ids=["first"],
               review={"matched_memory_ids": ["profile"],
                       "memory_matches": [{"memory_id": "profile", "card_ids": ["first", "duplicate"]}]})
    bucket = Counter()
    auditor.update_fact_retrieval(bucket, row, question)
    assert bucket["matched_facts"] == 1
    assert bucket["hit_at_5"] == 1
    auditor.update_fact_retrieval(bucket, {"grade_status": "error"}, question)
    assert bucket["unscored_expected_facts"] == 1
    assert bucket["matched_facts"] == 1
