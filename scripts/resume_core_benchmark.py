"""正式回答完成后，从复用或评分阶段恢复同一冻结评测版本。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.eval.core_benchmark import (
    ModelRegistry, atomic_json, check_run, grade_results, import_results,
    load_definitions, production_fingerprints, reuse_definitions, run_reuse_case,
)
from agent.eval.core_benchmark_report import build_report
from agent.types.eval.core_benchmark import BenchmarkConfig


def resume(root: Path) -> None:
    root = root.resolve()
    check_run(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["pilot"] or manifest.get("case_ids"):
        raise ValueError("此入口只恢复完整正式题库")
    config = BenchmarkConfig(**manifest["config"])
    definitions = load_definitions(pilot=False)
    if manifest["dataset_hashes"] != {d.dataset_id: d.fingerprint() for d in definitions}:
        raise ValueError("题库指纹变化，必须新建运行版本")
    expected = {
        (d.dataset_id, c.case_id, q.question_id, repeat, condition)
        for d in definitions for c in d.cases for q in c.questions
        for repeat in range(1, config.repeats + 1)
        for condition in ("P0M0", "P0M1", "P1M0", "P1M1")
    }
    actual = set()
    for path in (root / "cases").glob("**/result.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        actual.add(tuple(value[k] for k in ("dataset_id", "case_id", "question_id", "repeat", "condition")))
    if actual != expected:
        raise ValueError("正式回答尚未完整，请使用完整运行器恢复")
    delegate = ModelRegistry.default().get()
    if (getattr(delegate, "name", "unknown") != manifest["provider"]
            or getattr(delegate, "model_name", "unknown") != manifest["model"]
            or hashlib.sha256(str(getattr(delegate, "base_url", "")).encode()).hexdigest() != manifest["endpoint_hash"]):
        raise ValueError("模型配置变化，必须新建运行版本")
    provenance = dict(
        driver=str(Path(__file__).relative_to(ROOT)),
        driver_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        original_evaluation_hash=manifest["evaluation_hash"],
        config=asdict(config),
        formal_answers_reused=len(actual),
        note="仅跳过已完整的正式阶段；调用冻结版本的复用、评分及报告函数。",
    )
    atomic_json(root / "stage-resume-manifest.json", provenance)
    print(f"[resume] 正式回答 {len(actual)} 已完整，保留全部已有结果", flush=True)
    reuse = reuse_definitions(definitions) if manifest["include_reuse"] else ()
    jobs = [(d, c, r) for d in reuse for c in d.cases for r in range(1, config.repeats + 1)
            if any(not (root / "reuse" / c.case_id / f"repeat-{r}" / name / "sequence.json").exists()
                   for name in ("P0M1", "P1M1"))]
    print(f"[resume] 未完整复用场景轮次 {len(jobs)}", flush=True)
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        futures = [pool.submit(run_reuse_case, d, c, root, delegate, config, r) for d, c, r in jobs]
        for future in as_completed(futures):
            future.result()
            check_run(root)
            print(f"[resume] 复用已保存 {len(list((root / 'reuse').glob('**/answer.json')))}", flush=True)
    check_run(root)
    print("[resume] 开始冻结答案盲评及分层复核", flush=True)
    grade_results(root, (*definitions, *reuse), delegate, config)
    check_run(root)
    import_results(root, (*definitions, *reuse), config)
    build_report(root)
    atomic_json(root / "completed.json", dict(
        formal_answers=len(list((root / "cases").glob("**/result.json"))),
        reuse_answers=len(list((root / "reuse").glob("**/result.json"))),
        production_unchanged=production_fingerprints() == manifest["production"],
        phase_resume_driver=provenance,
    ))
    print("[resume] 评测、数据库导入及报告完成", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    resume(parser.parse_args().output)
