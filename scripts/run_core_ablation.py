"""在冻结长对话上运行两个核心模块的四组消融，不修改生产配置或旧结果。"""
from __future__ import annotations
import argparse
import copy
import json
import sqlite3
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.eval.long_dialogue import RecordedModel, replay, _components, _previous_history, _review, _implementation_hash
from agent.eval.datasets.long_dialogues import load_long_datasets
from agent.core.agent_loop import AgentLoop
from agent.core.agent_loop_trace import AgentLoopTrace
from agent.core.event_bus import EventBus
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.perception.perception_anchor import ModelAnchorBuilder
from agent.core.perception.perception_trace import PerceptionTrace
from agent.core.perception.token_estimator import SafeEstimator
from agent.core.tool_dispatch import ToolDispatch
from agent.core.tool_handler import ToolHandler
from agent.model.registry import ModelRegistry
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.context import ContextBudget
from agent.types.conversation import Message
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import TurnRecord
from agent.types.memory.categories import UserCardStatus
from agent.types.perception.admission import AdmissionPolicy
from agent.types.perception.result import PerceptionConfig, PerceptionMetrics, PerceptionResult, PerceptionRecord
from agent.types.perception.trace import normalized_usage
from agent.types.runtime import LoopConfig

CASES = ("combined_profession_phone", "combined_laptop_scope", "combined_svg_failure",
         "combined_backup_mixed", "third_party_profile", "anchor_reuse", "retention_update")
CONDITIONS = ((False, False), (False, True), (True, False), (True, True))


class FullInput(PerceptionBuilder):
    """无perception对照：只做协议组装与预算核对，不分级、过期裁剪或生成Anchor。"""
    def __init__(self, *args, previous=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.previous = previous

    def build(self, session, **kwargs):
        view = session.model_copy(deep=True)
        view.messages = copy.deepcopy([message for previous in self.previous for message in previous.messages] + session.messages)
        view.tool_calls = copy.deepcopy([tool for previous in self.previous for tool in previous.tool_calls] + session.tool_calls)
        for index, message in enumerate(view.messages):
            message.seq_id = index
        view.context_anchor, view.context_verdicts = None, {}
        loaded = self._load(view, memory_snapshot=kwargs.get("memory_snapshot"),
                            skill_selection=kwargs.get("skill_selection"), ephemeral_prompt=kwargs.get("ephemeral_prompt"))
        messages = self._build_messages(loaded.items, ())
        estimator = SafeEstimator()
        cost = estimator.estimate_messages(messages) + estimator.estimate_tools(kwargs.get("tool_schemas") or ())
        budget = self.config.budget
        if cost > budget.max_input_tokens:
            raise ValueError("完整历史对照超过模型请求预算")
        records = tuple(PerceptionRecord(item_name=item.name, priority=item.priority,
                         token_estimate=estimator.estimate_messages([item.message]), decision="selected",
                         reason="完整原文对照，未执行perception", kind=item.kind, role=item.message["role"],
                         content_chars=len(str(item.message.get("content", ""))), origin_ref=item.origin_ref,
                         seq=item.seq) for item in loaded.items)
        result = PerceptionResult(messages=messages, records=records,
            metrics=PerceptionMetrics(trace_id=kwargs["trace_id"], budget=budget.max_input_tokens,
                tokens_used=cost, budget_usage=cost/budget.max_input_tokens, message_count=len(messages),
                total_chars=sum(len(str(message.get("content", ""))) for message in messages)),
            output_token_limit=budget.reserved_output_tokens)
        self._trace.record(result, iteration=kwargs.get("iteration"))
        return result


def costs(calls):
    output = {}
    for call in calls:
        item = output.setdefault(call["purpose"], dict(calls=0, input_tokens=0, output_tokens=0, unknown_usage=0, duration_ms=0))
        incoming, outgoing = normalized_usage(call.get("usage") or {})
        item["calls"] += 1
        item["input_tokens"] += incoming or 0
        item["output_tokens"] += outgoing or 0
        item["unknown_usage"] += incoming is None or outgoing is None
        item["duration_ms"] += call.get("duration_ms", 0)
    return output


def answer(storage, session, scope, case, question, directory, delegate, *, perception, memory):
    model = RecordedModel(delegate, directory / "model-calls.jsonl")
    retriever, tools, context = _components(storage, directory, scope, model, question)
    if not memory:
        # 保留原文读回，只关闭长期卡片，避免同时消融perception依赖的检索接口。
        for card in storage.memory.search(scope, "", limit=100, as_of_ms=int(question.asked_at.timestamp()*1000)):
            storage.memory.put(card.model_copy(update={"status": UserCardStatus.QUARANTINED,
                               "revision": card.revision + 1}), expected_revision=card.revision)
    trace = PerceptionTrace()
    observer = AgentLoopTrace(EventBus(), trace)
    handler = ToolHandler(ToolDispatch(observer, registry=tools), registry=tools)
    budget = ContextBudget(model_context_tokens=20000 if perception else 250000, reserved_output_tokens=1500)
    options = dict(trace=trace, anchor_builder=ModelAnchorBuilder(model), admission_policy=AdmissionPolicy(mode="off"),
                   config=PerceptionConfig(budget=budget))
    engine = PerceptionBuilder(**options) if perception else FullInput(**options, previous=_previous_history(storage,scope,case,question,session))
    loop = AgentLoop(model, perception=engine, tool_handler=handler, trace=observer,
                     config=LoopConfig(max_iterations=10, model_retries=1))
    originals = [message.model_dump(mode="json") for message in session.messages]
    snapshot = context.freeze(scope, session_id=session.session_id, query=question.text) if memory else None
    user = Message(role="user", content_text=f"当前对话时间：{question.asked_at.isoformat()}\n{question.text}")
    result = loop.run_turn(session,user,memory_scope=scope,memory_snapshot=snapshot)
    observed = trace.get(result.turn_id)
    turn = TurnRecord(turn_id=result.turn_id, session_id=session.session_id, scope=scope, user_message_id=user.message_id,
        assistant_message_ids=tuple(message.message_id for message in session.messages if message.role=="assistant" and message.seq_id>user.seq_id),
        tool_call_ids=tuple(tool.tool_call_id for tool in session.tool_calls if tool.trajectory_id==result.turn_id),
        started_at=user.created_at, finished_at=__import__("time").time(), completed=result.completed,
        interrupted=result.interrupted, error=result.error, perception_trace=observed.model_dump(mode="json"),
        input_token_count=observed.input_token_count, output_token_count=observed.output_token_count)
    storage.sessions.commit_turn(session, turn, scope=scope)
    trace.finish(result.turn_id)
    return dict(actual_answer=result.final_text or "", completed=result.completed, error=result.error,
                input_tokens=observed.input_token_count, output_tokens=observed.output_token_count,
                iterations=len(observed.builds), trace=observed.model_dump(mode="json"),
                raw_unchanged=originals==[message.model_dump(mode="json") for message in session.messages[:len(originals)]],
                calls=model.calls), model, retriever


def run_case(case, root, delegate):
    question = next(question for question in case.questions if question.question_id=="q2")
    directory = root / case.case_id
    directory.mkdir()
    storage = RuntimeStorage.open(directory / "seed.db")
    scope = MemoryScope(tenant_id="core-ablation", user_id=case.case_id)
    extractor = RecordedModel(delegate, directory / "extraction-calls.jsonl")
    session, source_map, tool_map = replay(case, question, storage, scope, extractor, directory)
    cards = storage.memory.search(scope, "", limit=100, as_of_ms=int(question.asked_at.timestamp()*1000))
    with storage.db.read() as conn:
        jobs = [(row[0], json.loads(row[1] or "{}")) for row in conn.execute("SELECT status,result_json FROM jobs")]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    conditions = []
    memory_review = None
    for perception, memory in CONDITIONS:
        name = f"P{int(perception)}M{int(memory)}"
        destination = directory / name
        destination.mkdir()
        with closing(sqlite3.connect(storage.db.path)) as source, closing(sqlite3.connect(destination/"agent.db")) as target:
            source.backup(target)
        current = RuntimeStorage.open(destination/"agent.db")
        result, model, retriever = answer(current, session.model_copy(deep=True), scope, case, question, destination,
                                           delegate, perception=perception, memory=memory)
        actual_cards = cards if memory else ()
        review = _review(model, case, question, result["actual_answer"], actual_cards, source_map)
        result.update(condition=name, perception=perception, memory=memory, review=review, costs=costs(model.calls))
        result.pop("calls")
        (destination/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        conditions.append(result)
        if memory and not perception:
            memory_review = review
    matched_cards = {identity for item in (memory_review or {}).get("memory_matches", []) for identity in item["card_ids"]}
    retrieval = MemoryRetrieverForProbe(storage, scope, question, matched_cards)
    output = dict(case_id=case.case_id, question_id=question.question_id, question_hash=question.fingerprint(),
        target=case.target, history_turns=sum(len(history.turns) for history in case.history_for(question)),
        conditions=conditions, expected_memories=len(question.expected_active_memory_ids or ()),
        matched_memories=len((memory_review or {}).get("matched_memory_ids", [])),
        card_count=len(cards), matched_card_count=len(matched_cards),
        extra_card_ids=(memory_review or {}).get("extra_card_ids",[]), forbidden_card_ids=(memory_review or {}).get("forbidden_card_ids",[]),
        cards=[card.model_dump(mode="json") for card in cards], source_message_id_map=source_map, source_tool_call_id_map=tool_map,
        extraction_complete=all(status=="completed" for status,_ in jobs),
        parse_clean=all(not result.get("review",{}).get("extraction_issues") for _,result in jobs),
        integrity_ok=integrity=="ok" and not foreign_keys,
        extraction_costs=costs(extractor.calls), retrieval_probe=retrieval, implementation_hash=_implementation_hash())
    (directory/"result.json").write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"[ablation] {case.case_id} recall={output['matched_memories']}/{output['expected_memories']}",flush=True)
    return output


def MemoryRetrieverForProbe(storage, scope, question, matched_cards):
    """独立测量自然问题的卡片排序，未主动调用工具不等于没有召回能力。"""
    from agent.storage.sqlite.memory_retrieval import MemoryRetriever
    retriever = MemoryRetriever(storage.db, now_ms=lambda:int(question.asked_at.timestamp()*1000))
    hits = retriever.search(scope, question.text, source="card", limit=10)
    return dict(query=question.text, expected_card_ids=sorted(matched_cards),
                top5_card_ids=[hit.reference.removeprefix("card:") for hit in hits[:5]],
                top10_card_ids=[hit.reference.removeprefix("card:") for hit in hits],
                recalled_at_5=len(matched_cards & {hit.reference.removeprefix("card:") for hit in hits[:5]}),
                recalled_at_10=len(matched_cards & {hit.reference.removeprefix("card:") for hit in hits}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--workers",type=int,default=4)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True,exist_ok=False)
    definitions = load_long_datasets()
    manifest = dict(implementation_hash=_implementation_hash(), dataset_hashes={definition.dataset_id:definition.fingerprint() for definition in definitions},
        cases=list(CASES), question="q2", conditions=["P0M0","P0M1","P1M0","P1M1"],
        memory_ablation="关闭长期卡片及经验自动注入；保留原文检索接口", perception_ablation="完整跨会话原文协议组装，与生产perception对照；JEV排除")
    (root/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    selected = [case for definition in definitions for case in definition.cases if case.case_id in CASES]
    delegate = ModelRegistry.default().get()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_case,case,root,delegate) for case in selected]
        for future in as_completed(futures):
            results.append(future.result())
            (root/"report.json").write_text(json.dumps(dict(manifest=manifest,results=results),ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(dict(cases=len(results),conditions=sum(len(case["conditions"]) for case in results)),ensure_ascii=False))


if __name__=="__main__":
    main()
