"""Verify persisted replay originals and actual turn traces against frozen fixtures."""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent.eval.datasets.long_dialogues import load_long_datasets


def audit(root):
    root=Path(root).resolve()
    definitions={case.case_id:case for dataset in load_long_datasets() for case in dataset.cases}
    checks=[]
    for path in sorted(root.glob("*__q*/result.json")):
        result=json.loads(path.read_text(encoding="utf-8"))
        case=definitions[result["case_id"]]
        question=next(q for q in case.questions if q.question_id==result["question_id"])
        expected=[turn for session in case.history_for(question) for turn in session.turns]
        messages=result.get("source_message_id_map",{})
        tools=result.get("source_tool_call_id_map",{})
        failures=[]
        with sqlite3.connect(path.parent/"agent.db") as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
                failures.append("integrity_check")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                failures.append("foreign_key_check")
            if set(messages)!={turn.turn_id for turn in expected}:
                failures.append("history_cutoff_or_source_map")
            for turn in expected:
                row=conn.execute("SELECT role,content_text,message_json FROM messages WHERE message_id=?",
                                 (messages.get(turn.turn_id),)).fetchone()
                if (not row or row[:2]!=("user",turn.user_text)
                    or json.loads(row[2]).get("content_text")!=turn.user_text):
                    failures.append("user_original:"+turn.turn_id)
                for observation in turn.tool_results:
                    row=conn.execute("SELECT tool_call_json FROM tool_calls WHERE tool_call_id=?",
                                     (tools.get(observation.tool_call_id),)).fetchone()
                    if not row:
                        failures.append("tool_original_missing:"+observation.tool_call_id)
                        continue
                    actual=json.loads(row[0])
                    fields={"tool_name":observation.name,"status":observation.status,
                            "result_text":observation.result_text,"result_data":observation.result_data,
                            "error":observation.error}
                    if any(actual.get(key)!=value for key,value in fields.items()):
                        failures.append("tool_original_changed:"+observation.tool_call_id)
            optimized=result.get("optimized") or {}
            if optimized.get("trace"):
                row=conn.execute("SELECT turn_json,input_token_count,output_token_count FROM turns WHERE turn_id=?",
                                 (optimized["trace"]["trace_id"],)).fetchone()
                # Trace identity may use turn_id, depending on the public trace type.
                if row is None:
                    rows=conn.execute("SELECT turn_json,input_token_count,output_token_count FROM turns").fetchall()
                    row=next((r for r in rows if json.loads(r[0]).get("perception_trace")==optimized["trace"]),None)
                if not row or json.loads(row[0]).get("perception_trace")!=optimized["trace"]:
                    failures.append("actual_request_trace_persistence")
                elif row[1:]!=(optimized.get("input_token_count"),optimized.get("output_token_count")):
                    failures.append("actual_usage_columns")
        checks.append(dict(case_id=case.case_id,question_id=question.question_id,
                           user_originals=len(expected),tool_originals=sum(len(t.tool_results) for t in expected),
                           passed=not failures,failures=failures))
    return dict(root=str(root),questions=len(checks),passed=sum(c["passed"] for c in checks),checks=checks)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root",type=Path)
    args=parser.parse_args()
    report=audit(args.root)
    (args.root/"storage-audit.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="checks"},ensure_ascii=False))
    print(json.dumps([c for c in report["checks"] if not c["passed"]],ensure_ascii=False))
    raise SystemExit(0 if report["questions"] and report["passed"]==report["questions"] else 1)
