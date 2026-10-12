"""从实际消融结果生成指标、逐题对照及版本绑定的数据库记录。"""
import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent.eval.datasets.long_dialogues import load_long_datasets
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.memory.extraction import MEMORY_EXTRACTOR_VERSION


def summarize(root, import_db=None):
    root=Path(root).resolve()
    report=json.loads((root/"report.json").read_text(encoding="utf-8"))
    cases=report["results"]
    script_hash=hashlib.sha256((ROOT/"scripts/run_core_ablation.py").read_bytes()).hexdigest()
    groups={}
    for condition in ("P0M0","P0M1","P1M0","P1M1"):
        rows=[next(row for row in case["conditions"] if row["condition"]==condition) for case in cases]
        groups[condition]=dict(questions=len(rows),semantic_passed=sum(row["review"]["answer_passed"] for row in rows),
            main_input_tokens=sum(row["input_tokens"] or 0 for row in rows),
            main_output_tokens=sum(row["output_tokens"] or 0 for row in rows),
            main_calls=sum(row["costs"].get("main",{}).get("calls",0) for row in rows),
            anchor_input_tokens=sum(row["costs"].get("anchor",{}).get("input_tokens",0) for row in rows),
            anchor_output_tokens=sum(row["costs"].get("anchor",{}).get("output_tokens",0) for row in rows),
            unknown_usage=sum(cost["unknown_usage"] for row in rows for cost in row["costs"].values()),
            completed=sum(row["completed"] for row in rows),
            raw_unchanged=all(row["raw_unchanged"] for row in rows))
    expected=sum(case["expected_memories"] for case in cases)
    matched=sum(case["matched_memories"] for case in cases)
    card_count=sum(case["card_count"] for case in cases)
    matched_cards=sum(case["matched_card_count"] for case in cases)
    negatives=[case for case in cases if case["target"]!="perception" and case["expected_memories"]==0]
    memory=dict(expected_facts=expected,matched_facts=matched,extraction_recall=matched/expected if expected else None,
        committed_cards=card_count,matched_cards=matched_cards,card_precision=matched_cards/card_count if card_count else None,
        unmatched_cards=card_count-matched_cards,
        negative_cases=len(negatives),negative_cards=sum(case["card_count"] for case in negatives),
        forbidden_cards=sum(len(case["forbidden_card_ids"]) for case in cases),
        extra_cards=sum(len(case["extra_card_ids"]) for case in cases),
        conditional_retrieval_recall_at_5=sum(case["retrieval_probe"]["recalled_at_5"] for case in cases)/matched_cards if matched_cards else None,
        conditional_retrieval_recall_at_10=sum(case["retrieval_probe"]["recalled_at_10"] for case in cases)/matched_cards if matched_cards else None,
        extraction_input_tokens=sum(case["extraction_costs"].get("memory",{}).get("input_tokens",0) for case in cases),
        extraction_output_tokens=sum(case["extraction_costs"].get("memory",{}).get("output_tokens",0) for case in cases),
        extraction_calls=sum(case["extraction_costs"].get("memory",{}).get("calls",0) for case in cases),
        all_extraction_complete=all(case["extraction_complete"] for case in cases if case["target"]!="perception"),
        all_parse_clean=all(case["parse_clean"] for case in cases if case["target"]!="perception"),
        all_integrity_ok=all(case["integrity_ok"] for case in cases))
    comparisons=[]
    for memory_on in (0,1):
        base=groups[f"P0M{memory_on}"]["main_input_tokens"]
        optimized=groups[f"P1M{memory_on}"]["main_input_tokens"]
        comparisons.append(dict(memory_on=bool(memory_on),baseline_input=base,optimized_input=optimized,
            saved_input=base-optimized,saving=1-optimized/base,
            anchor_input=groups[f"P1M{memory_on}"]["anchor_input_tokens"],
            saving_after_anchor_input=1-(optimized+groups[f"P1M{memory_on}"]["anchor_input_tokens"])/base))
    per_case=[]
    for case in cases:
        rows={row["condition"]:row for row in case["conditions"]}
        base=rows["P0M1"]["input_tokens"]
        optimized=rows["P1M1"]["input_tokens"]
        per_case.append(dict(case_id=case["case_id"],history_turns=case["history_turns"],baseline_input=base,
                             optimized_input=optimized,saved_input=base-optimized,saving=1-optimized/base,
                             before_calls=rows["P0M1"]["iterations"],after_calls=rows["P1M1"]["iterations"]))
    summary=dict(cases=len(cases),answer_runs=sum(group["questions"] for group in groups.values()),groups=groups,
        perception_comparisons=comparisons,memory=memory,per_case=per_case,
        memory_input_change_with_perception=1-groups["P1M1"]["main_input_tokens"]/groups["P1M0"]["main_input_tokens"],
        implementation_hash=report["manifest"]["implementation_hash"],script_hash=script_hash,
        limitations=["7个合成长对话检查点、每条件一次，非96题全量最终验收或统计显著性证明。",
            "使用同一模型独立语义评分，存在评分模型偏差。", "Memory关闭指长期卡片与经验注入关闭，原文检索仍保留。",
            "提取召回、卡片识别精度、已提取正确卡片的检索Recall@K是三个不同指标。",
            "计数包含缓存输入token；输入/输出单独列出，不直接等价于费用。"])
    positive=[case for case in cases if case["expected_memories"]>0]
    positive_off=sum(next(row for row in case["conditions"] if row["condition"]=="P1M0")["input_tokens"] for case in positive)
    positive_on=sum(next(row for row in case["conditions"] if row["condition"]=="P1M1")["input_tokens"] for case in positive)
    summary["memory_positive_case_input_change"]=dict(cases=len(positive),without_cards=positive_off,
        with_cards=positive_on,saved_tokens=positive_off-positive_on,saving=1-positive_on/positive_off)
    summary["memory"]["perception_only_cases_extraction_not_activated"]=sum(case["target"]=="perception" for case in cases)
    (root/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["# 两个核心模块的真实消融", "", "同一冻结问题、同一模型、同一历史截止。P=perception，M=长期记忆卡片；M关闭时保留原文检索。",
        "", "| 条件 | 答案正确 | 主模型整轮输入 | 主模型输出 | 主调用数 | Anchor输入 | Anchor输出 |",
        "|---|---:|---:|---:|---:|---:|---:|"]
    lines.extend(f"| {name} | {row['semantic_passed']}/{row['questions']} | {row['main_input_tokens']:,} | {row['main_output_tokens']:,} | {row['main_calls']} | {row['anchor_input_tokens']:,} | {row['anchor_output_tokens']:,} |" for name,row in groups.items())
    lines.extend(("", "## Perception逐场景整轮输入", "", "| 场景 | 历史轮次 | 关闭P | 开启P | 节省 | 主调用数变化 |", "|---|---:|---:|---:|---:|---:|"))
    lines.extend(f"| {row['case_id']} | {row['history_turns']} | {row['baseline_input']:,} | {row['optimized_input']:,} | {row['saving']:.2%} | {row['before_calls']}→{row['after_calls']} |" for row in per_case)
    lines.extend(("",f"Memory提取召回 {matched}/{expected}；正确卡片 {matched_cards}/{card_count}；负例建卡 {memory['negative_cards']}；自然问题检索Recall@5/10均为 {memory['conditional_retrieval_recall_at_5']:.2%}。",
        f"提取/准入/修复合计调用 {memory['extraction_calls']} 次，输入 {memory['extraction_input_tokens']:,}、输出 {memory['extraction_output_tokens']:,} tokens，后台提取成本不能当作免费。", "", *summary["limitations"]))
    (root/"summary.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    if import_db:
        store=RuntimeStorage.open(import_db).evals
        definitions=load_long_datasets()
        indexed={case.case_id:(definition,case) for definition in definitions for case in definition.cases}
        for definition in definitions:
            store.publish(definition)
        imported=[]
        for name in groups:
            for definition in definitions:
                selected=[case for case in cases if indexed[case["case_id"]][0].dataset_id==definition.dataset_id]
                if not selected:
                    continue
                identity=uuid.uuid5(uuid.NAMESPACE_URL,str(root)+name+definition.dataset_id).hex
                store.start_run(run_id=identity,dataset_id=definition.dataset_id,dataset_version=definition.version,
                    provider="claude",model="deepseek-v4-flash",prompt_version="core-ablation-v1",
                    extractor_version=MEMORY_EXTRACTOR_VERSION,details={"condition":name,"manifest":report["manifest"],"script_hash":script_hash})
                for case in selected:
                    row=next(row for row in case["conditions"] if row["condition"]==name)
                    result={**row,"case_id":case["case_id"],"question_id":case["question_id"],"question_hash":case["question_hash"],
                        "source_message_id_map":case["source_message_id_map"],"source_tool_call_id_map":case["source_tool_call_id_map"],
                        "memory_metrics":{key:case[key] for key in ("expected_memories","matched_memories","card_count","matched_card_count","retrieval_probe")},
                        "artifact_directory":str(root/case["case_id"]/name),"implementation_hash":case["implementation_hash"]}
                    store.put_result(identity,case["case_id"],case["question_id"],
                                     status="passed" if row["completed"] and row["review"]["answer_passed"] else "failed",result=result)
                store.finish_run(identity,dict(condition=name,questions=len(selected)))
                imported.append(identity)
        (root/"database-import.json").write_text(json.dumps(dict(database=str(Path(import_db).resolve()),run_ids=imported),indent=2),encoding="utf-8")
    return summary


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root",type=Path)
    parser.add_argument("--import-db",type=Path)
    args=parser.parse_args()
    report=summarize(args.root,args.import_db)
    print(json.dumps({key:value for key,value in report.items() if key not in {"per_case","limitations"}},ensure_ascii=False))
