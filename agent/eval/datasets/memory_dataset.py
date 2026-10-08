"""Memory 案例的导入、发布和评测转换。

职责：保留基础案例快照，读取日常黄金案例，以明确版本发布到 SQLite。
边界：考题答案只供评分；不会加入用户历史或模型输入，也不会成为记忆卡。
关联：memory_eval_types.py 校验定义；MemoryEvalStore 存储；memory_behavior.py 执行。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from pathlib import Path

from .types.memory_eval_types import MemoryEvalDataset


_FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "eval" / "datasets"


def baseline_definition(legacy: dict) -> MemoryEvalDataset:
    cases = []
    for original in legacy["cases"]:
        sessions = original.get("sessions") or [original["turns"]]
        history = [dict(session_id=f"s{index + 1}", turns=[
            dict(turn_id=f"s{index + 1}.t{ordinal + 1}", user_text=text)
            for ordinal, text in enumerate(turns)]) for index, turns in enumerate(sessions)]
        memories = [dict(memory_id=f"m{index + 1}", kind=memory["kind"],
                         claim="；".join(group[0] for group in memory["anchors"]),
                         anchors=memory["anchors"], query=memory["query"],
                         outcome_status=memory.get("outcome_status"))
                    for index, memory in enumerate(original["expected"])]
        questions = []
        if original.get("probe"):
            probe = original["probe"]
            questions.append(dict(question_id="q1", text=probe["text"],
                reference_answer="；".join(group[0] for group in probe["anchors"]),
                required_meaning=["根据历史回答，满足原基础用例的事实断言"],
                answer_contains=probe["anchors"], required_tools=probe["tools"],
                requires_semantic_review=False))
        cases.append(dict(case_id=original["id"], title=original["description"],
                          description=original["description"], scenario="basic_regression", difficulty="basic",
                          abilities=["extraction", "retrieval"], history=history,
                          expected_memories=memories, questions=questions))
    return MemoryEvalDataset.model_validate(dict(
        dataset_id="memory_behavior", version="1.0.0", title="Memory 基础回归案例",
        description="现有 23 例的固定快照；验证分类、空提取、入库、检索、更正与删除。",
        created_at="2026-10-08T10:00:00+08:00", cases=cases,
    ))


def load_defaults() -> tuple[MemoryEvalDataset, MemoryEvalDataset]:
    basic = baseline_definition(json.loads((_FIXTURES / "memory_behavior.json").read_text(encoding="utf-8")))
    golden = MemoryEvalDataset.model_validate_json((_FIXTURES / "memory_golden_v2.json").read_text(encoding="utf-8"))
    return basic, golden


def to_behavior_suite(dataset: MemoryEvalDataset) -> dict:
    """每个黄金问题独立回放历史，避免前一道问题或回答污染后一道题。"""
    cases = []
    for definition in dataset.cases:
        for question in definition.questions or (None,):
            count = (question.history_session_count if question else None) or len(definition.history)
            prefix = definition.history[:count]
            available = {turn.turn_id for session in prefix for turn in session.turns}
            memories = [memory for memory in definition.expected_memories
                        if set(memory.evidence_turn_ids) <= available]
            wire = dict(id=definition.case_id if len(definition.questions) <= 1 else
                        f"{definition.case_id}__{question.question_id}",
                        description=definition.title,
                        sessions=[[turn.user_text for turn in session.turns] for session in prefix],
                        expected=[dict(kind=memory.kind.value, anchors=memory.anchors,
                                       query=memory.query, outcome_status=memory.outcome_status.value
                                       if memory.outcome_status else None) for memory in memories],
                        strict_card_count=definition.strict_card_count,
                        forbidden_memories=[rule.model_dump(mode="json") for rule in definition.forbidden_memories],
                        case_id=definition.case_id, case_hash=definition.fingerprint(),
                        history_definition=[session.model_dump(mode="json") for session in prefix])
            if question:
                wire["probe"] = dict(text=question.text, anchors=question.answer_contains,
                                     lacks=question.answer_lacks, tools=question.required_tools)
                wire["golden_question"] = question.model_dump(mode="json")
            cases.append(wire)
    return dict(name=dataset.dataset_id, version=dataset.version,
                dataset_id=dataset.dataset_id, dataset_version=dataset.version,
                dataset_hash=dataset.fingerprint(), schema_version=dataset.schema_version, cases=cases)


def publish_defaults(db_path: Path) -> dict:
    from ...storage.sqlite import RuntimeStorage

    definitions = load_defaults()  # 完整验证后才对现有库进行任何写入。
    target = Path(db_path).resolve()
    backup = None
    if target.is_file() and target.stat().st_size:
        backup = target.with_name(f"{target.stem}.pre_eval_datasets.{uuid.uuid4().hex[:8]}.db")
        with sqlite3.connect(target) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
    store = RuntimeStorage.open(target).memory_evals
    published = [dict(version=data.version, inserted=store.publish(data)) for data in definitions]
    return dict(database=str(target), backup=str(backup) if backup else None,
                published=published, datasets=store.list_datasets("memory_behavior"))


def main() -> int:
    from ...storage.sqlite.database import Database
    from ...storage.sqlite.memory_eval_store import SqliteMemoryEvalStore

    parser = argparse.ArgumentParser(description="Memory 评测案例版本管理，不调用模型")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[3] / "data/agent.db")
    parser.add_argument("command", choices=["publish-defaults", "list", "export"])
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "publish-defaults":
        result = publish_defaults(args.db)
    else:
        store = SqliteMemoryEvalStore(Database(args.db))
        if args.command == "list":
            result = store.list_datasets("memory_behavior")
        else:
            if not args.version or not args.output:
                parser.error("export 需要 --version 和 --output")
            dataset = store.get_dataset("memory_behavior", args.version)
            if dataset is None:
                parser.error("指定案例版本不存在")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
            result = dict(version=dataset.version, output=str(args.output))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
