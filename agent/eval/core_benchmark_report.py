"""从真实回答、usage和冻结标准生成四组消融与场景聚类区间。"""
from __future__ import annotations
import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from agent.types.perception.trace import normalized_usage


def ratio(a,b):
    return a/b if b else None


def answer_correct(row):
    review=row.get("review",{})
    return bool(row.get("completed") and review.get("answer_passed") and not review.get("violations"))


def usage_summary(calls):
    incoming,outgoing=[],[]
    for call in calls:
        a,b=normalized_usage(call.get("usage") or {})
        incoming.append(a);outgoing.append(b)
    return dict(calls=len(calls),input_tokens=sum(incoming) if all(v is not None for v in incoming) else None,output_tokens=sum(outgoing) if all(v is not None for v in outgoing) else None,known_input_tokens=sum(v for v in incoming if v is not None),known_output_tokens=sum(v for v in outgoing if v is not None),missing_input_usage=sum(v is None for v in incoming),missing_output_usage=sum(v is None for v in outgoing),duration_ms=sum(call.get("duration_ms",0) for call in calls),failed_calls=sum(bool(call.get("error")) for call in calls))


def row_cost(row):
    calls=row.get("model_calls",[])
    main=[call for call in calls if call.get("purpose")=="main"]
    auxiliary=[call for call in calls if call.get("purpose")!="main"]+row.get("source",{}).get("source_calls",[])
    product=usage_summary(main+auxiliary)
    return dict(main=usage_summary(main),auxiliary=usage_summary(auxiliary),product=product,judge=usage_summary(row.get("review_calls",[])+row.get("second_review_calls",[])),readback_calls=len([call for call in row.get("tool_calls",[]) if call.get("tool_name") in {"memory_read","memory_search"}]))


def cost_total(rows,stage="main",field="input_tokens"):
    values=[row_cost(row)[stage][field] for row in rows]
    return sum(values) if values and all(value is not None for value in values) else None


def clustered_interval(values,*,seed=20261011,samples=2000):
    groups=list(values.values())
    if len(groups)<2:return None
    rng=random.Random(seed);means=[]
    for _ in range(samples):
        selected=[groups[rng.randrange(len(groups))] for _ in groups]
        means.append(sum(sum(group) for group in selected)/sum(len(group) for group in selected))
    means.sort()
    return [means[int(samples*.025)],means[min(samples-1,int(samples*.975))]]


def compare(rows,memory,config):
    off={ (row["case_id"],row["question_id"],row["repeat"]):row for row in rows if row["condition"]==f"P0M{memory}"}
    on={ (row["case_id"],row["question_id"],row["repeat"]):row for row in rows if row["condition"]==f"P1M{memory}"}
    keys=sorted(set(off)&set(on));clusters=defaultdict(list)
    for key in keys:clusters[key[0]].append(int(answer_correct(on[key]))-int(answer_correct(off[key])))
    ci=clustered_interval(clusters,seed=config["random_seed"],samples=config["bootstrap_samples"])
    quality_off=ratio(sum(answer_correct(off[key]) for key in keys),len(keys));quality_on=ratio(sum(answer_correct(on[key]) for key in keys),len(keys))
    verdict="证据不足" if ci is None or ci[0]<-config["noninferiority_margin"] else "支持非劣"
    if ci and ci[1]<-config["noninferiority_margin"]:verdict="不满足非劣"
    missing_pairs=len(set(off)^set(on))
    unscored=sum(off[key].get("grade_status")=="error" or on[key].get("grade_status")=="error" for key in keys)
    if missing_pairs or unscored:verdict="证据不足"
    def costs(selected):
        before=[off[key] for key in selected];after=[on[key] for key in selected]
        a,b=cost_total(before),cost_total(after)
        pa,pb=cost_total(before,"product"),cost_total(after,"product")
        oa,ob=cost_total(before,"product","output_tokens"),cost_total(after,"product","output_tokens")
        total_a=pa+oa if pa is not None and oa is not None else None
        total_b=pb+ob if pb is not None and ob is not None else None
        return dict(pairs=len(selected),main_off=a,main_on=b,main_input_saving=1-b/a if a and b is not None else None,product_input_off=pa,product_input_on=pb,product_input_saving=1-pb/pa if pa and pb is not None else None,output_off=oa,output_on=ob,product_total_tokens_off=total_a,product_total_tokens_on=total_b,product_total_token_saving=1-total_b/total_a if total_a and total_b is not None else None)
    per_case={}
    for identity in sorted(clusters):per_case[identity]=costs([key for key in keys if key[0]==identity])
    high=[key for key in keys if off[key]["high_noise"]]
    return dict(pairs=len(keys),missing_pairs=missing_pairs,unscored_pairs=unscored,scenes=len(clusters),quality_off=quality_off,quality_on=quality_on,quality_difference=quality_on-quality_off if quality_on is not None else None,clustered_95_ci=ci,noninferiority=verdict,all=costs(keys),high_noise=costs(high),by_length={str(n):costs([key for key in keys if off[key]["length"]==n]) for n in (40,80,120)},per_case=per_case,saving_win_rate=ratio(sum(item["main_input_saving"]>0 for item in per_case.values() if item["main_input_saving"] is not None),sum(item["main_input_saving"] is not None for item in per_case.values())))


def memory_summary(rows):
    metrics=[row["memory_metrics"] for row in rows if row.get("memory_metrics") is not None]
    expected=sum(item["expected_facts"] for item in metrics);matched=sum(item["matched_facts"] for item in metrics)
    cards=sum(item["cards"] for item in metrics);matched_cards=sum(item["matched_cards"] for item in metrics)
    bad=sum(len(set(item["extra_card_ids"])|set(item["forbidden_card_ids"])) for item in metrics)
    unscored=sum(item.get("unscored_cards",0) for item in metrics)
    precision=ratio(cards-bad-unscored,cards);recall=ratio(matched,expected)
    negative=[item for item in metrics if item["negative_case"]]
    retrieval=sum(item["retrieval_expected_cards"] for item in metrics)
    return dict(checkpoints=len(metrics),expected_facts=expected,matched_facts=matched,missed_facts=expected-matched,extraction_recall=recall,cards=cards,matched_cards=matched_cards,card_precision=precision,matched_gold_card_precision=ratio(matched_cards,cards),card_f1=2*precision*recall/(precision+recall) if precision is not None and recall is not None and precision+recall else None,unsupported_cards=bad,unscored_cards=unscored,negative_checkpoints=len(negative),negative_false_card_checkpoints=sum(item["cards"]>0 for item in negative),negative_false_positive_rate=ratio(sum(item["cards"]>0 for item in negative),len(negative)),retrieval_recall_at_5=ratio(sum(item["retrieval_at_5"] for item in metrics),retrieval),retrieval_recall_at_10=ratio(sum(item["retrieval_at_10"] for item in metrics),retrieval),retrieval_denominator=retrieval,forbidden_card_count=sum(len(item["forbidden_card_ids"]) for item in metrics),classification_correct=sum(item.get("classification_correct",0) for item in metrics),classification_evaluated=sum(item.get("classification_evaluated",0) for item in metrics),classification_accuracy=ratio(sum(item.get("classification_correct",0) for item in metrics),sum(item.get("classification_evaluated",0) for item in metrics)))


def build_report(root):
    root=Path(root);manifest=json.loads((root/"manifest.json").read_text(encoding="utf-8"));config=manifest["config"]
    formal=[json.loads(path.read_text(encoding="utf-8")) for path in (root/"cases").glob("**/result.json")]
    warm=[json.loads(path.read_text(encoding="utf-8")) for path in (root/"reuse").glob("**/result.json")]
    groups={}
    for condition in ("P0M0","P0M1","P1M0","P1M1"):
        rows=[row for row in formal if row["condition"]==condition]
        groups[condition]=dict(answers=len(rows),correct=sum(answer_correct(row) for row in rows),accuracy=ratio(sum(answer_correct(row) for row in rows),len(rows)),passed=sum(row.get("passed",False) for row in rows),grade_errors=sum(row.get("grade_status")!="completed" for row in rows),memory=memory_summary(rows),main_input_tokens=cost_total(rows),product_input_tokens=cost_total(rows,"product"),main_output_tokens=cost_total(rows,"main","output_tokens"))
    comparisons={}
    for target in ("all","perception","memory","combined"):
        rows=formal if target=="all" else [row for row in formal if row["target"]==target]
        comparisons[target]={f"M{m}":compare(rows,m,config) for m in (0,1)}
    curves=[]
    for condition in ("P0M1","P1M1"):
        for step in (1,3,6):
            rows=[row for row in warm if row["condition"]==condition and row["warm_step"]<=step]
            curves.append(dict(condition=condition,through_question=step,answers=len(rows),accuracy=ratio(sum(answer_correct(row) for row in rows),len(rows)),main_input_tokens=cost_total(rows),product_input_tokens=cost_total(rows,"product"),product_output_tokens=cost_total(rows,"product","output_tokens")))
    calls=[]
    for path in root.glob("**/*calls.jsonl"):
        calls.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    stages={purpose:usage_summary([call for call in calls if call.get("purpose")==purpose]) for purpose in sorted({call.get("purpose","unknown") for call in calls})}
    failed=[{key:row.get(key) for key in ("case_id","question_id","repeat","condition","target","family","error","grading_error","review","hard_checks","artifact_directory")} for row in formal+warm if not row.get("passed")]
    hard=Counter(key for row in formal+warm for key,ok in row.get("hard_checks",{}).items() if not ok)
    special={family:dict(answers=len(rows),answer_violations=sum(bool(row.get("review",{}).get("violations")) for row in rows),forbidden_cards=sum(len(row.get("review",{}).get("forbidden_card_ids",[])) for row in rows)) for family in ("correction","projects","forget","expiry","success","failure","mixed","outcomes") if (rows:=[row for row in formal+warm if row["family"]==family])}
    report=dict(formal_answers=len(formal),reuse_answers=len(warm),planned_formal=240 if manifest["pilot"] else 4320,planned_reuse=0 if manifest["pilot"] or not manifest["include_reuse"] else 864,groups=groups,perception=comparisons,reuse_cost_curve=curves,actual_call_totals_by_stage=stages,hard_check_failures=dict(hard),special_violations=special,reviewed_twice=sum("second_review" in row for row in formal+warm),review_disagreements=sum(row.get("review_disagreement",False) for row in formal+warm),failed_results=len(failed),limitations=["同一配置模型作为裁判，两次盲评不等同于人工独立审查。","usage缺失时相应总量与节省为null；known量只作为不完整诊断。","冷启动产品输入包含该检查点前累计记忆提炼的归属成本；实际调用总表按唯一日志统计。","token输入输出分别报告；未配置单价时不能推断货币节省。","跨会话场景中，perception保留生产会话策略，单独于纯perception单会话组报告。"])
    report["complete_coverage"]=len(formal)==report["planned_formal"] and len(warm)==report["planned_reuse"]
    report["failure_categories"]=dict(Counter(row.get("error") or row.get("grading_error") or "semantic_or_memory_violation" for row in formal+warm if not row.get("passed")))
    report["memory_by_family"]={name:memory_summary([row for row in formal if row["family"]==name and row["target"]!="perception" and row["condition"]=="P0M1"]) for name in sorted({row["family"] for row in formal if row["target"]!="perception"})}
    for group in groups.values():group["quality_floor_passed"]=group["accuracy"] is not None and group["accuracy"]>=config["quality_floor"] and not group["grade_errors"]
    for item in comparisons["perception"].values():item["high_noise_saving_target_passed"]=item["high_noise"]["main_input_saving"] is not None and item["high_noise"]["main_input_saving"]>=config["main_input_saving"]
    for condition in ("P0M1","P1M1"):
        selected=[row for row in formal if row["condition"]==condition and row["target"]!="perception"]
        metrics=[row["memory_metrics"] for row in selected if row.get("memory_metrics")]
        summary=memory_summary(selected);expected=summary["expected_facts"]
        summary.update(end_to_end_recall_at_5=ratio(sum(item.get("end_to_end_at_5",0) for item in metrics),expected),end_to_end_recall_at_10=ratio(sum(item.get("end_to_end_at_10",0) for item in metrics),expected),classification_missing_checkpoints=sum(item.get("classification_missing",False) for item in metrics))
        report.setdefault("memory_core",{})[condition]=summary
    report["served_models"]=dict(Counter(call.get("served_model") or "unknown" for call in calls))
    (root/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (root/"failures.json").write_text(json.dumps(failed,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    csv_rows=[]
    for row in formal+warm:
        cost=row_cost(row);csv_rows.append(dict(case_id=row["case_id"],question_id=row["question_id"],repeat=row["repeat"],condition=row["condition"],target=row["target"],length=row["length"],warm_step=row.get("warm_step"),correct=answer_correct(row),main_input=cost["main"]["input_tokens"],main_output=cost["main"]["output_tokens"],aux_input=cost["auxiliary"]["input_tokens"],aux_output=cost["auxiliary"]["output_tokens"],product_input=cost["product"]["input_tokens"],readback_calls=cost["readback_calls"],duration_ms=row.get("duration_ms")))
    for name,rows in (("answers.csv",csv_rows),("reuse-cost-curve.csv",curves)):
        if rows:
            with (root/name).open("w",encoding="utf-8-sig",newline="") as handle:
                writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    def pct(value):return "未知" if value is None else f"{value:.2%}"
    lines=[f"# {'试运行' if manifest['pilot'] else '正式'}长对话四组评测",f"实际回答：{len(formal)}；连续复用：{len(warm)}。", "", "| 条件 | 正确 / 回答 | 正确率 | 主输入token | 冷启动产品输入token |", "|---|---:|---:|---:|---:|"]
    for name,group in groups.items():lines.append(f"| {name} | {group['correct']}/{group['answers']} | {pct(group['accuracy'])} | {group['main_input_tokens']} | {group['product_input_tokens']} |")
    lines.extend(("","| 纯perception比较 | 正确率差 | 场景聚类95%区间 | 非劣结论 | 高干扰主输入节省 | 含辅助输入节省 |","|---|---:|---|---|---:|---:|"))
    for name,item in comparisons["perception"].items():lines.append(f"| {name} | {pct(item['quality_difference'])} | {item['clustered_95_ci']} | {item['noninferiority']} | {pct(item['high_noise']['main_input_saving'])} | {pct(item['all']['product_input_saving'])} |")
    lines.extend(("","| memory组 | 提取Recall | 卡片Precision / F1 | 误存卡 | 负例误建率 | Recall@5 / @10 |","|---|---:|---:|---:|---:|---:|"))
    for condition in ("P0M1","P1M1"):
        item=memory_summary([row for row in formal if row['condition']==condition and row['target'] in {'memory','combined'}]);lines.append(f"| {condition} | {pct(item['extraction_recall'])} | {pct(item['card_precision'])} / {pct(item['card_f1'])} | {item['unsupported_cards']} | {pct(item['negative_false_positive_rate'])} | {pct(item['retrieval_recall_at_5'])} / {pct(item['retrieval_recall_at_10'])} |")
    lines.extend(("",f"硬检查失败：{dict(hard)}。重复盲评分歧：{report['review_disagreements']}/{report['reviewed_twice']}。", "",*report["limitations"]))
    (root/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    render_curves(root,curves)
    return report


def render_curves(root,curves):
    """科学绘图只使用实际完整usage，未知值保持空缺。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import pyplot as plt
    except ImportError:return
    if not any(row["answers"] for row in curves):return
    figure,axes=plt.subplots(2,1,figsize=(8,7),sharex=True)
    for condition in ("P0M1","P1M1"):
        rows=[row for row in curves if row["condition"]==condition and row["answers"]]
        x=[row["through_question"] for row in rows]
        axes[0].plot(x,[row["accuracy"] for row in rows],marker="o",label=condition)
        axes[1].plot(x,[row["product_input_tokens"] for row in rows],marker="o",label=condition)
    axes[0].axhline(.9,color="grey",linestyle="--",label="90% quality floor")
    axes[0].set_ylabel("Cumulative answer accuracy");axes[0].set_ylim(0,1.02)
    axes[1].set_ylabel("Cumulative product input tokens");axes[1].set_xlabel("Through question")
    axes[1].set_xticks([1,3,6])
    for axis in axes:axis.grid(alpha=.25);axis.legend()
    figure.tight_layout();figure.savefig(root/"reuse-cost-curve.svg");plt.close(figure)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("root",type=Path)
    build_report(parser.parse_args().root)
