"""在独立评分版本修复工具 context 解析，只复评失败评分，冻结实际回答。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.eval import core_benchmark as core
from agent.eval.datasets.core_benchmark import load_definitions
from agent.types.eval.core_benchmark import BenchmarkConfig
from agent.types.memory.items import UserMemoryItem


def evidence_sources(sources, observed_tools):
    """按真实接口读取 context 对象或相邻摘要列表，只接受成功读回的结构引用。"""
    result = set(sources)
    for call in observed_tools:
        if call.get("status") != "success":
            continue
        data = call.get("result_data") or {}
        if not isinstance(data, dict):
            continue
        refs = [call.get("readback_reference"), data.get("reference")]
        context = data.get("context")
        contexts = context if isinstance(context, list) else [context]
        for entry in contexts:
            if isinstance(entry, dict):
                refs.extend((entry.get("source_ref"), entry.get("reference")))
                refs.extend(entry.get("evidence_refs") or ())
        for hit in data.get("hits") or ():
            if isinstance(hit, dict):
                refs.append(hit.get("reference"))
                refs.extend(hit.get("evidence_refs") or ())
        if any(isinstance(ref, str) and ref.split(":", 1)[-1] in sources for ref in refs):
            result.add(call["tool_call_id"])
    return result


ORIGINAL_VALIDATE = core._validate_review


def validate_review(review, candidates, card_ids, answer):
    result = ORIGINAL_VALIDATE(review, candidates, card_ids, answer)
    matches = result.get("classification_matches", [])
    if (not isinstance(matches, list)
            or any(not isinstance(item, dict) or not isinstance(item.get("memory_id"), str)
                   or not isinstance(item.get("card_ids"), list)
                   or any(not isinstance(value, str) for value in item["card_ids"])
                   for item in matches)):
        raise ValueError("classification_matches 必须是含 memory_id 和 card_ids 的对象数组")
    return result


def prepare(source, output, manifest):
    """复制结果和裁判日志；只对已结束的不可变产品日志建立硬链接。"""
    path = output / "manifest.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("评分设施或输入版本改变，必须新建评分目录")
        return
    output.mkdir(parents=True, exist_ok=True)
    for folder in ("cases", "reuse"):
        for item in (source / folder).glob("**/result.json"):
            target = output / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    for pattern in ("**/*calls.jsonl", "**/inflight/*.json", "code-snapshot/**/*.py"):
        for item in source.glob(pattern):
            target = output / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            if "review" in item.name:
                shutil.copy2(item, target)
            else:
                if not target.exists():
                    os.link(item, target)
                elif not os.path.samefile(item, target):
                    raise ValueError("中断复制后的目标与原始不可变日志不一致")
    for name in ("preflight.json", "production-freeze.json"):
        if (source / name).exists():
            shutil.copy2(source / name, output / name)
    core.atomic_json(path, manifest)


def rescore(source: Path, output: Path):
    source, output = source.resolve(), output.resolve()
    if source == output or source in output.parents:
        raise ValueError("评分目录必须独立于原回答目录")
    original = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if core.production_fingerprints() != original["production"] or core.evaluation_fingerprint() != original["evaluation_hash"]:
        raise ValueError("受测代码或原评测代码指纹变化")
    config = BenchmarkConfig(**original["config"])
    definitions = load_definitions(pilot=original["pilot"])
    reuse = core.reuse_definitions(definitions) if original["include_reuse"] else ()
    lookup = {(d.dataset_id, c.case_id): (c, {q.question_id: q for q in c.questions})
              for d in (*definitions, *reuse) for c in d.cases}
    manifest = dict(original, scoring_version="core-benchmark-score-v2",
                    answer_run=str(source), answer_manifest_hash=hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
                    scoring_code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    scoring_changes=["context 列表按结构读取来源", "类别匹配结构错误进入既有两次重试"],
                    answer_policy="实际回答、卡片快照、原文、标准答案和生产代码均不修改；只复评失败评分")
    prepare(source, output, manifest)
    paths = [*(output / "cases").glob("**/result.json"), *(output / "reuse").glob("**/result.json")]
    if len(paths) != 5184:
        raise ValueError("完整回答矩阵应为 5184 条")
    delegate = core.ModelRegistry.default().get()
    if (delegate.name != original["provider"] or delegate.model_name != original["model"]
            or hashlib.sha256(str(getattr(delegate, "base_url", "")).encode()).hexdigest() != original["endpoint_hash"]):
        raise ValueError("模型配置改变")
    core.evidence_sources = evidence_sources
    core._validate_review = validate_review

    def review_one(path, secondary=False):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not secondary and value.get("grade_status") == "completed":
            return
        if secondary and "second_review" in value:
            return
        case, questions = lookup[value["dataset_id"], value["case_id"]]
        question = questions[value["question_id"]]
        cards = tuple(UserMemoryItem.model_validate(card) for card in value["cards"])
        history_key = "secondary_scoring_history" if secondary else "scoring_history"
        value.setdefault(history_key, []).append({k: value[k] for k in (
            "grade_status", "grading_error", "review", "second_review_error", "memory_metrics", "passed") if k in value})
        try:
            review, calls = core.blind_review(delegate, path.parent / ("second-review-calls.jsonl" if secondary else "review-calls.jsonl"),
                                              case, question, value["actual_answer"], cards,
                                              value["source"]["source_map"], value["source"]["tool_map"],
                                              review_index=1 if secondary else 0, observed_tools=value.get("tool_calls", ()))
            if secondary:
                value.update(second_review=review, second_review_calls=calls,
                             review_disagreement=any(review.get(k) != value.get("review", {}).get(k) for k in (
                                 "answer_passed", "matched_memory_ids", "forbidden_card_ids", "extra_card_ids", "violations")))
                value.pop("second_review_error", None)
            else:
                metrics = core.memory_metrics(case, question, cards, review, None, None,
                                              refs=value.get("retrieval_ids", [])) if value["condition"].endswith("M1") else None
                value.update(review=review, review_calls=calls, memory_metrics=metrics, grade_status="completed")
                value["passed"] = bool(value["completed"] and value["hard_checks"] and all(value["hard_checks"].values())
                                       and review["answer_passed"] and not review["violations"] and not review["forbidden_card_ids"])
                value.pop("grading_error", None)
                value["scoring_version"] = manifest["scoring_version"]
        except Exception as exc:
            if secondary:
                value["second_review_error"] = f"{type(exc).__name__}: {exc}"
            else:
                value.update(grade_status="error", grading_error=f"{type(exc).__name__}: {exc}")
        core.atomic_json(path, value)

    primary = []
    secondary = []
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("grade_status") != "completed":
            primary.append(path)
        if "second_review" not in value and (value.get("grade_status") != "completed" or value.get("second_review_error")):
            secondary.append(path)
    print(f"[rescore] 复评失败主评分 {len(primary)}；失败/缺失复核 {len(secondary)}；原有效评分保持", flush=True)
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        for future in as_completed([pool.submit(review_one, p) for p in primary]):
            future.result()
    print("[rescore] 修复评分完成，开始修复复核", flush=True)
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        for future in as_completed([pool.submit(review_one, p, True) for p in secondary]):
            future.result()
    core.atomic_json(output / "rescoring-completed.json", dict(primary_retried=len(primary), secondary_retried=len(secondary),
                                                              actual_answers_reused=len(paths), production_unchanged=core.production_fingerprints() == original["production"]))
    print("[rescore] 完成；原回答版本和原评分历史保留", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rescore(args.source, args.output)
