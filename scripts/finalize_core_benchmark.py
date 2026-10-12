"""流式裁去报告不用的请求正文，避免数 GB 原始记录同时驻留内存。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.eval import core_benchmark_report
from agent.eval.core_benchmark import atomic_json, check_run, import_results, production_fingerprints, reuse_definitions
from agent.eval.datasets.core_benchmark import load_definitions
from agent.types.eval.core_benchmark import BenchmarkConfig
from scripts.audit_core_benchmark import audit


def compact_call(call):
    return {k: v for k, v in call.items() if k not in {"messages", "tools", "response", "tool_calls"}}


def compact_loads(text, *args, **kwargs):
    value = json.loads(text, *args, **kwargs)
    if not isinstance(value, dict):
        return value
    if "case_id" in value and "condition" in value and "question_id" in value:
        for key in ("trace", "actual_answer", "cards", "scope", "retrieval_ids"):
            value.pop(key, None)
        value["source"] = {"source_calls": [compact_call(c) for c in value.get("source", {}).get("source_calls", [])]}
        for key in ("model_calls", "review_calls", "second_review_calls"):
            value[key] = [compact_call(c) for c in value.get(key, [])]
    elif "purpose" in value and "call_id" in value:
        value = compact_call(value)
    return value


def build_compact_report(root):
    # 只在本进程的报告模块替换解析入口；原始日志和冻结实现文件保持原样。
    original = core_benchmark_report.json
    core_benchmark_report.json = SimpleNamespace(loads=compact_loads, dumps=json.dumps)
    try:
        return core_benchmark_report.build_report(root)
    finally:
        core_benchmark_report.json = original


def certify_costs(root, report, integrity):
    """在原统计结果之外标记未返回和未归属成本，不将这些成本当零。"""
    uncertain = integrity["interrupted_usage_unknown"] + integrity["completed_product_calls_without_row_attribution_records"]
    affected = set()
    cold_uncertain = False
    for call in uncertain:
        if call.get("purpose") == "review":
            continue
        parts = Path(call["path"]).parts
        if parts and parts[0] == "reuse":
            condition = next((part for part in parts if part in {"P0M1", "P1M1"}), None)
            affected.update((condition,) if condition else ("P0M1", "P1M1"))
        elif parts and parts[0] == "cases":
            cold_uncertain = True
    report["completed_call_totals_by_stage"] = report["actual_call_totals_by_stage"]
    report["actual_call_totals_by_stage"] = integrity["actual_unique_call_totals_by_stage"]
    report["cost_integrity"] = dict(full_product_cost_known=integrity["full_product_cost_known"],
                                    interrupted_calls=len(integrity["interrupted_usage_unknown"]),
                                    unattributed_completed_calls=integrity["completed_product_calls_without_row_attribution"],
                                    cold_cost_uncertain=cold_uncertain, reuse_cost_uncertain_conditions=sorted(affected),
                                    method="复用累计成本只要有归属不完整调用，就保守标记未知；已返回数值另列 known 字段。")
    report["memory_fact_retrieval"] = integrity.get("memory_fact_retrieval", {})
    for curve in report["reuse_cost_curve"]:
        if curve["condition"] in affected:
            for name in ("product_input_tokens", "product_output_tokens"):
                curve["known_" + name] = curve[name]
                curve[name] = None
    if cold_uncertain:
        for group in report["groups"].values():
            group["known_product_input_tokens"] = group["product_input_tokens"]
            group["product_input_tokens"] = None
        for comparison in report["perception"].values():
            for item in comparison.values():
                for costs in (item["all"], item["high_noise"], *item["by_length"].values(), *item["per_case"].values()):
                    for field in list(costs):
                        if field.startswith("product_") or field in {"output_off", "output_on"}:
                            costs["known_" + field] = costs[field]
                            costs[field] = None
    caveat = (f"完整成本审计：遗留 {len(integrity['interrupted_usage_unknown'])} 个未返回调用，"
              f"另有 {integrity['completed_product_calls_without_row_attribution']} 个已返回产品调用未归属到答案。"
              "未知 usage 不按零计算；受影响复用累计产品成本与净节省保持未知，known 字段仅为已记录量。")
    report["limitations"].append(caveat)
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (root / "report.md").open("a", encoding="utf-8") as handle:
        handle.write("\n" + caveat + "\n")
    curves = report["reuse_cost_curve"]
    if curves:
        fields = list(dict.fromkeys(k for row in curves for k in row))
        with (root / "reuse-cost-curve.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(curves)
    # 逐次表保留已返回成本，受影响序列增加完整度字段。
    with (root / "answers.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames + ["known_product_input", "product_cost_complete"]
        rows = list(reader)
    for row in rows:
        incomplete = (bool(row["warm_step"]) and row["condition"] in affected) or (not row["warm_step"] and cold_uncertain)
        row["known_product_input"] = row["product_input"]
        row["product_cost_complete"] = not incomplete and bool(row["product_input"])
        if incomplete:
            row["product_input"] = ""
    with (root / "answers.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    core_benchmark_report.render_curves(root, curves)
    return report


def finalize(root: Path):
    root = root.resolve()
    check_run(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for folder in ("cases", "reuse"):
        for path in (root / folder).glob("**/result.json"):
            status = json.loads(path.read_text(encoding="utf-8")).get("grade_status")
            if status not in {"completed", "error"}:
                raise ValueError("仍有尚未执行的评分，请先恢复评分阶段")
    definitions = load_definitions(pilot=manifest["pilot"])
    reuse = reuse_definitions(definitions) if manifest["include_reuse"] else ()
    config = BenchmarkConfig(**manifest["config"])
    import_results(root, (*definitions, *reuse), config)
    report = build_compact_report(root)
    integrity = audit(root)
    report = certify_costs(root, report, integrity)
    if not report["complete_coverage"] or not integrity["complete_answer_coverage"]:
        raise ValueError("回答矩阵不完整，已保留部分报告但不能标记完成")
    provenance = dict(
        script=str(Path(__file__).relative_to(ROOT)),
        code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        evaluation_hash=manifest["evaluation_hash"],
        method="报告只移除统计未使用的请求正文与 trace，保留 usage、评分、工具及失败字段。",
    )
    atomic_json(root / "report-finalize-manifest.json", provenance)
    atomic_json(root / "completed.json", dict(
        formal_answers=report["formal_answers"], reuse_answers=report["reuse_answers"],
        production_unchanged=production_fingerprints() == manifest["production"],
        evaluation_unchanged=integrity["evaluation_unchanged"],
        grade_errors=integrity["grades"].get("error", 0),
        full_product_cost_known=integrity["full_product_cost_known"],
        phase_resume_driver=json.loads((root / "stage-resume-manifest.json").read_text(encoding="utf-8")) if (root / "stage-resume-manifest.json").exists() else None,
        report_finalize=provenance,
    ))
    print(json.dumps({k: report[k] for k in ("formal_answers", "reuse_answers", "complete_coverage")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    finalize(parser.parse_args().root)
