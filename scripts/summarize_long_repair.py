"""Produce reviewable quality and actual-usage comparisons without altering answers."""
import argparse
import json
import sys
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent.eval.datasets.long_dialogues import load_long_datasets
from agent.types.perception.trace import normalized_usage


def first_input(result):
    for build in (result.get("trace") or {}).get("builds",[]):
        for call in build.get("model_calls",[]):
            if call.get("input_token_count") is not None:
                return call["input_token_count"]
    return None


def call_costs(attempts):
    costs = {}
    for attempt in attempts:
        for call in attempt.get("model_calls", []):
            purpose = call["purpose"]
            if purpose == "memory":
                prompt = str((call.get("messages") or [{}])[0].get("content", ""))
                purpose = ("memory_repair" if "有限格式/来源修复" in prompt else
                           "memory_admission" if "独立准入复核" in prompt else "memory_extraction")
            item = costs.setdefault(purpose, dict(calls=0, known_input_tokens=0, known_output_tokens=0,
                                                  unknown_usage_calls=0, model_duration_ms=0))
            incoming, outgoing = normalized_usage(call.get("usage") or {})
            item["calls"] += 1
            item["known_input_tokens"] += incoming or 0
            item["known_output_tokens"] += outgoing or 0
            item["unknown_usage_calls"] += incoming is None or outgoing is None
            item["model_duration_ms"] += call.get("duration_ms", 0)
    return costs


def question_tools(directory, response, filename):
    trace = (response or {}).get("trace") or {}
    identity = trace.get("trace_id")
    path = directory / filename
    if not identity or not path.is_file():
        return None
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT turn_id FROM turns WHERE json_extract(turn_json, '$.perception_trace.trace_id')=?",
                           (identity,)).fetchone()
        if row is None:
            return None
        calls = [json.loads(row[0]) for row in conn.execute("SELECT tool_call_json FROM tool_calls WHERE turn_id=?", (row[0],))]
    return dict(names=dict(Counter(call["tool_name"] for call in calls)),
                memory_read_calls=sum(call["tool_name"] == "memory_read" for call in calls),
                failed_calls=sum(call["status"] == "failed" for call in calls),
                p3_hit_refs=list(trace.get("p3_hit_refs", [])))


def summarize(root):
    root=Path(root)
    results=[json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*__q*/result.json"))]
    definitions={(case.case_id,q.question_id):(case,q) for dataset in load_long_datasets()
                 for case in dataset.cases for q in case.questions}
    expected=sum(len(definitions[(r["case_id"],r["question_id"])][1].expected_active_memory_ids or ()) for r in results)
    matched=sum(r.get("matched_memories",0) for r in results)
    review_complete=sum(bool(r.get("semantic_review")) for r in results)
    semantic=sum((r.get("semantic_review") or {}).get("answer_passed",False) for r in results)
    pairs=[]
    for r in results:
        optimized=first_input(r.get("optimized") or {})
        baseline=first_input(r.get("baseline") or {})
        if optimized is not None and baseline:
            total_optimized = (r.get("optimized") or {}).get("input_token_count")
            total_baseline = (r.get("baseline") or {}).get("input_token_count")
            pairs.append(dict(case_id=r["case_id"],question_id=r["question_id"],optimized_input=optimized,
                              baseline_input=baseline,saving=1-optimized/baseline,
                              estimated_saving=r.get("input_saving"),
                              optimized_total_input=total_optimized, baseline_total_input=total_baseline,
                              total_saving=1-total_optimized/total_baseline if total_optimized is not None and total_baseline else None))
    failures=[dict(case_id=r["case_id"],question_id=r["question_id"],error=r.get("error") or (r.get("optimized") or {}).get("error"),
                   failed_checks=[key for key,value in r.get("hard_checks",{}).items() if not value],
                   reason=(r.get("semantic_review") or {}).get("reason"),reruns=[a["passed"] for a in r.get("reruns",[])])
              for r in results if not r["passed"]]
    reruns = [attempt for result in results for attempt in result.get("reruns", [])]
    tools = [dict(case_id=r["case_id"], question_id=r["question_id"],
                  optimized=question_tools(root / f"{r['case_id']}__{r['question_id']}", r.get("optimized"), "agent.db"),
                  baseline=question_tools(root / f"{r['case_id']}__{r['question_id']}", r.get("baseline"), "baseline.db"))
             for r in results]
    report=dict(questions=len(results),semantic_reviews=review_complete,semantic_correct=semantic,
                semantic_accuracy=semantic/len(results) if results else None,
                expected_memories=expected,matched_memories=matched,memory_recall=matched/expected if expected else None,
                actual_input_comparisons=pairs,failures=failures,
                call_costs=dict(initial=call_costs(results), reruns=call_costs(reruns)),
                question_tools=tools,
                attempt_duration_ms=dict(initial=sum(r.get("duration_ms", 0) for r in results),
                                         reruns=sum(r.get("duration_ms", 0) for r in reruns)))
    (root/"quality-comparison.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["# 长对话质量与实际用量对照",f"运行目录：{root.resolve()}",
           f"题目 {len(results)}；有效语义复核 {review_complete}；答案正确 {semantic}。",
           f"冻结标准应记事实 {expected}；已匹配实际卡片事实 {matched}。",
           "召回分母按冻结题目计算，异常题不能从分母中移除。初次失败不能因复跑通过而改写。",
           "", "| 场景 / 题目 | 首次实际输入：优化 / 基线 | 首次节省 | 估算节省 | 整轮实际输入：优化 / 基线 | 整轮节省 |",
           "|---|---:|---:|---:|---:|---:|"]
    lines.extend(f"| {p['case_id']} / {p['question_id']} | {p['optimized_input']} / {p['baseline_input']} | {p['saving']:.1%} | {p['estimated_saving']:.1%} | "
                 f"{p['optimized_total_input']} / {p['baseline_total_input']} | "
                 f"{format(p['total_saving'], '.1%') if p['total_saving'] is not None else '未知'} |"
                 for p in pairs if p['estimated_saving'] is not None)
    lines.extend(("", "## 实际调用成本", "",
                  "用量来自provider usage；未知用量另计，不能视为零。main包含优化和完整历史基线。耗时是调用合计，不能视为并行运行的墙钟时间。",
                  "", "| 尝试 | 用途 | 调用数 | 已知输入 tokens | 已知输出 tokens | 未知用量调用 | 调用耗时 ms |",
                  "|---|---|---:|---:|---:|---:|---:|"))
    for phase, costs in report["call_costs"].items():
        lines.extend(f"| {phase} | {purpose} | {c['calls']} | {c['known_input_tokens']} | {c['known_output_tokens']} | {c['unknown_usage_calls']} | {c['model_duration_ms']} |"
                     for purpose, c in costs.items())
    optimized_tools = [item["optimized"] for item in tools if item["optimized"] is not None]
    lines.extend(("", f"优化问题工具记录可核对 {len(optimized_tools)}/{len(results)}；实际memory_read调用合计 "
                  f"{sum(item['memory_read_calls'] for item in optimized_tools)}，P3读回引用合计 "
                  f"{sum(len(item['p3_hit_refs']) for item in optimized_tools)}。逐题记录见quality-comparison.json。"))
    lines.extend(("","## 初次失败与两次复跑","","| 场景 / 题目 | 错误或未通过的检查 | 复跑结果 |","|---|---|---|"))
    lines.extend(f"| {f['case_id']} / {f['question_id']} | {f['error'] or ', '.join(f['failed_checks']) or '语义或召回未通过'} | {f['reruns']} |" for f in failures)
    (root/"quality-comparison.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return {k:v for k,v in report.items() if k not in {"actual_input_comparisons","failures","question_tools","call_costs"}}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root",type=Path)
    args=parser.parse_args()
    print(json.dumps(summarize(args.root),ensure_ascii=False))
