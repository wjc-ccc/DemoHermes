"""确认低内存汇总与冻结统计实现产生相同数值，并保留原始调用。"""
import json
from dataclasses import asdict

from agent.eval.core_benchmark_report import build_report
from agent.types.eval.core_benchmark import BenchmarkConfig
from scripts.finalize_core_benchmark import build_compact_report, certify_costs


def test_compact_report_matches_original_statistics_and_preserves_logs(tmp_path):
    manifest = dict(pilot=True, include_reuse=False, config=asdict(BenchmarkConfig(repeats=1)))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    original_files = {}
    for scene in ("a", "b"):
        for condition in ("P0M0", "P0M1", "P1M0", "P1M1"):
            directory = tmp_path / "cases" / scene / condition
            directory.mkdir(parents=True)
            main = dict(call_id=scene + condition, purpose="main", served_model="fixture",
                        messages=[{"role": "user", "content": "测试原文"}], response="实际回答",
                        tools=[{"name": "memory_read"}], tool_calls=[],
                        usage={"prompt_tokens": 100 if condition[1] == "0" else 60, "completion_tokens": 9}, duration_ms=20)
            row = dict(case_id=scene, question_id="q1", repeat=1, condition=condition,
                       target="perception", family="head", length=40, high_noise=True,
                       actual_answer="实际回答", completed=True, grade_status="completed", passed=True,
                       review={"answer_passed": True, "violations": []}, hard_checks={"raw_unchanged": True},
                       trace={"messages": ["原始 trace"]}, cards=[], scope={}, retrieval_ids=[],
                       source={"source_calls": [], "source_map": {"a": "original"}},
                       model_calls=[main], tool_calls=[{"tool_name": "memory_read"}], duration_ms=20)
            for name, content in (("result.json", json.dumps(row, ensure_ascii=False)),
                                  ("model-calls.jsonl", json.dumps(main, ensure_ascii=False) + "\n")):
                path = directory / name
                path.write_text(content, encoding="utf-8")
                original_files[path] = path.read_bytes()
    expected = build_report(tmp_path)
    observed = build_compact_report(tmp_path)
    assert observed == expected
    assert observed["perception"]["perception"]["M0"]["all"]["main_input_saving"] == .4
    assert all(path.read_bytes() == content for path, content in original_files.items())


def test_unreturned_shared_memory_keeps_reuse_product_cost_unknown(tmp_path):
    report = dict(actual_call_totals_by_stage={}, groups={}, perception={}, limitations=[],
                  reuse_cost_curve=[dict(condition="P0M1", through_question=1, answers=1,
                                         accuracy=1, main_input_tokens=100, product_input_tokens=150, product_output_tokens=5)])
    integrity = dict(interrupted_usage_unknown=[dict(purpose="memory", path="reuse/case/repeat-1/source/inflight/id.json")],
                     completed_product_calls_without_row_attribution_records=[],
                     completed_product_calls_without_row_attribution=0, full_product_cost_known=False,
                     actual_unique_call_totals_by_stage={"memory": {"input_tokens": None}})
    (tmp_path / "report.md").write_text("原始统计\n", encoding="utf-8")
    (tmp_path / "answers.csv").write_text("condition,warm_step,product_input\nP0M1,1,150\n", encoding="utf-8-sig")
    certify_costs(tmp_path, report, integrity)
    curve = report["reuse_cost_curve"][0]
    assert curve["main_input_tokens"] == 100
    assert curve["product_input_tokens"] is None
    assert curve["known_product_input_tokens"] == 150
    assert report["cost_integrity"]["reuse_cost_uncertain_conditions"] == ["P0M1", "P1M1"]
    assert report["actual_call_totals_by_stage"]["memory"]["input_tokens"] is None
