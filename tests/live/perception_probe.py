"""显式运行的真实模型探针，不由 pytest 收集；只使用合成对话和隔离 SQLite。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import provider
from agent.core.building_messages import build_assistant_message, build_tool_message
from agent.core.perception.perception_admission import mark_readback_miss
from agent.core.perception.perception_anchor import ModelAnchorBuilder, anchor_facts
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.model.registry import ModelRegistry
from agent.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.context import ContextBudget, render_anchor
from agent.types.conversation import Message, ToolCall
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import TurnRecord
from agent.types.model import ToolCallRequest
from agent.types.perception.admission import AdmissionBand, AdmissionPolicy


def long_output(head, size=7000):
    padding = "archived diagnostic row: segment completed, no new requirement; "
    return head + "\n" + (padding * (size // len(padding) + 1))[:size]


def add_turn(storage, session, scope, user_text, answer, *, body=None, error=False):
    number = len(storage.executions.list_turns(session.session_id)) + 1
    turn_id = f"{session.session_id}-t{number}"
    user = session.add_message(Message(role="user", content_text=user_text, trajectory_id=turn_id))
    assistant_ids, call_ids = [], []
    if body is not None:
        call_id = f"{turn_id}-tool"
        request = ToolCallRequest(id=call_id, name="bash" if error else "read_file",
                                  arguments={"path": f"fixtures/report-{number}.txt"})
        request.arguments_raw = json.dumps(request.arguments)
        assistant = session.add_message(build_assistant_message("", [request]))
        assistant.trajectory_id = turn_id
        assistant_ids.append(assistant.message_id)
        call = ToolCall(tool_call_id=call_id, session_id=session.session_id, trajectory_id=turn_id,
                        tool_name=request.name, arguments=request.arguments, status="error" if error else "success",
                        result_text=None if error else body, error=body if error else None)
        session.tool_calls.append(call)
        observation = session.add_message(build_tool_message(call))
        observation.trajectory_id = turn_id
        call_ids.append(call_id)
    assistant = session.add_message(Message(role="assistant", content_text=answer, trajectory_id=turn_id))
    assistant_ids.append(assistant.message_id)
    storage.sessions.commit_turn(session, TurnRecord(
        turn_id=turn_id, session_id=session.session_id, scope=scope, user_message_id=user.message_id,
        assistant_message_ids=tuple(assistant_ids), tool_call_ids=tuple(call_ids),
        started_at=user.created_at, finished_at=assistant.created_at, completed=True, interrupted=False, error=None,
    ), scope=scope)
    return user, assistant


class RecordedModel:
    def __init__(self, delegate, output):
        self.delegate = delegate
        self.calls = []
        self.output = output

    def chat(self, messages, tools=None, **kwargs):
        print("[live] 调用 Anchor 模型", flush=True)
        started = time.perf_counter()
        constraints = {"rendered": messages[1]["content"].split("## Existing anchor")[0]}
        try:
            response = self.delegate.chat(messages, tools=tools)
        except Exception as exc:
            detail = str(exc)
            for key in (provider.JEVMODEL_API_KEY, provider.DEEPSEEK_API_KEY, provider.ANTHROPIC_API_KEY):
                if key:
                    detail = detail.replace(key, "[REDACTED]")
            self.calls.append({"latency_ms": int((time.perf_counter() - started) * 1000),
                               "error_type": type(exc).__name__, "error": detail,
                               "constraints": constraints})
            self.output.write_text(json.dumps(self.calls, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[live] Anchor 调用失败: {type(exc).__name__}", flush=True)
            raise
        self.calls.append({"latency_ms": int((time.perf_counter() - started) * 1000),
                           "usage": response.usage, "response": response.text,
                           "constraints": constraints})
        self.output.write_text(json.dumps(self.calls, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[live] Anchor 返回 {len(response.text)} 字符", flush=True)
        return response


def probe_admission(storage, scope):
    session = storage.sessions.get_or_create("live-jev", scope=scope)
    for index, head in enumerate((
        "Archived legacy UI inventory: old blue buttons and obsolete asset paths.",
        "SQLite sessions must survive restart. Current retry retention is 30 days.",
        "An unrelated completed build log for an abandoned demo module.",
    ), 1):
        add_turn(storage, session, scope, f"检查报告 {index}", "报告已查看。", body=long_output(head, 3200))
    add_turn(storage, session, scope, "接下来只关注 SQLite 对话持久化。", "继续检查会话恢复。")
    add_turn(storage, session, scope, "不再处理旧界面和演示模块。", "范围已收敛。")
    session.add_message(Message(role="user", content_text="验证 SQLite 对话在重启后恢复，保持 30 天重试保留期。"))
    original = [message.model_dump(mode="json") for message in session.messages]
    judge = provider.build_context_judge()
    policy = AdmissionPolicy(mode="shadow", min_chars=512, edge_chars=120,
        bands=(AdmissionBand(usage=0.01, defer_below=0.35, trim_below=0.75),))
    budget = ContextBudget(model_context_tokens=100512, reserved_output_tokens=512)
    print("[live] JEV: 降低准入触发阈值到 1%，调用真实 Judge", flush=True)
    shadow = PerceptionBuilder(context_judge=judge, admission_policy=policy).build(session, budget=budget)
    session.context_verdicts = shadow.verdicts
    active = PerceptionBuilder(context_judge=judge, admission_policy=policy.model_copy(update={"mode": "active"})).build(session, budget=budget)
    assert [message.model_dump(mode="json") for message in session.messages] == original
    assert shadow.metrics.tokens_used >= active.metrics.tokens_used
    readback = None
    rejected = next((verdict for verdict in active.verdicts.values() if verdict.action != "keep"), None)
    if rejected is not None:
        reference = f"tool:{rejected.tool_call_id}"
        raw = next(call.result_text for call in session.tool_calls if call.tool_call_id == rejected.tool_call_id)
        page = MemoryRetriever(storage.db).read(scope, reference, limit=10000)
        assert page is not None and page.content == raw
        session.context_verdicts, pinned = mark_readback_miss(active.verdicts, reference,
            applied_message_ids=active.applied_admission_message_ids)
        assert pinned is not None and pinned.missed and pinned.action == "keep"
        restored = PerceptionBuilder(admission_policy=policy.model_copy(update={"mode": "active"})).build(session, budget=budget)
        assert any(message.get("role") == "tool" and message.get("content") == raw for message in restored.messages)
        readback = {"reference": reference, "chars": len(raw), "sqlite_exact": True, "pinned_full_text": True}
    judge_verdicts = sum(verdict.source == "judge" for verdict in shadow.verdicts.values())
    return {"live_judge_ok": judge_verdicts > 0 and judge_verdicts == shadow.metrics.admission_judged_count and shadow.metrics.admission_judge_failures == 0,
            "shadow": shadow.metrics.model_dump(), "active": active.metrics.model_dump(),
            "verdicts": [value.model_dump(mode="json") for value in active.verdicts.values()],
            "source_unchanged": True, "readback": readback}


def probe_anchor(storage, scope):
    session = storage.sessions.get_or_create("live-anchor", scope=scope)
    add_turn(storage, session, scope, "Orion project: all conversations must persist in SQLite. Initial retention is 7 days.",
             "SQLite persistence is required.", body=long_output("Design record: SQLite storage is required."))
    add_turn(storage, session, scope, "Correction: retention is now 30 days; the previous 7-day setting is obsolete.",
             "The current approved retention is 30 days.", body=long_output("Configuration updated to retention_days=30."))
    add_turn(storage, session, scope, "Run the migration verification; keep any failure visible.",
             "Migration verification failed and still needs a fix.", body=long_output("pytest migration_check FAILED: missing index idx_session_scope."), error=True)
    add_turn(storage, session, scope, "Inspect an old unrelated UI inventory; do not treat it as a new requirement.",
             "Old UI inventory has no bearing on the storage task.", body=long_output("Legacy UI inventory only.", 4000))
    add_turn(storage, session, scope, "Memory can be disabled; SQLite persistence must still work.", "Both switches preserve SQLite conversations.")
    add_turn(storage, session, scope, "Action moved into core/action. Its capabilities must remain intact.", "Action execution behavior remains unchanged.")
    session.add_message(Message(role="user", content_text="Continue Orion. State the database, current retention, and migration verification status."))
    original = [message.model_dump(mode="json") for message in session.messages]
    model = RecordedModel(ModelRegistry.default().get(), storage.db.path.parent / "anchor-calls.json")
    budget = ContextBudget(model_context_tokens=12024, reserved_output_tokens=1024)
    builder = PerceptionBuilder(anchor_builder=ModelAnchorBuilder(model))
    print("[live] Anchor: 使用真实默认模型，临时输入预算 11000", flush=True)
    first = builder.build(session, budget=budget, trace_id="live-anchor-created")
    assert first.anchor_updated and first.anchor is not None, first.metrics.model_dump()
    assert first.metrics.anchor_status == "created" and first.metrics.anchor_error_code is None
    assert first.anchor.covered_turn_count >= 3
    assert any(fact.status == "failed" for fact in first.anchor.decisions)
    allowed = {message.message_id for message in session.messages if message.seq_id <= first.anchor.covered_end_seq}
    assert all(set(fact.source_message_ids) <= allowed for fact in anchor_facts(first.anchor))
    for message in session.messages[-5:]:
        assert any(item.get("content") == message.content_text for item in first.messages)
    assert [message.model_dump(mode="json") for message in session.messages] == original
    session.context_anchor = first.anchor
    session.messages.pop()
    add_turn(storage, session, scope, "Continue Orion: confirm the plan.", "SQLite and 30 days remain approved; the migration check still failed.")
    restored = RuntimeStorage.open(storage.db.path).sessions.find(session.session_key)
    assert restored.context_anchor == first.anchor
    restored.add_message(Message(role="user", content_text="Continue the same project."))
    reused = builder.build(restored, budget=ContextBudget(model_context_tokens=50512, reserved_output_tokens=512))
    assert reused.metrics.anchor_status == "reused" and not reused.anchor_updated
    restored.messages.pop()
    add_turn(storage, restored, scope, "New correction: retention is now 45 days, overriding 30 days.",
             "45 days is the newest retention setting.", body=long_output("Approved new setting: retention_days=45.", 3700))
    add_turn(storage, restored, scope, "Inspect the migration fixture; its failed check still has not been fixed.",
             "The missing index remains unresolved.", body=long_output("Additional migration fixture inventory.", 3700))
    restored.add_message(Message(role="user", content_text="Answer in one sentence: database type, newest retention in days, and whether the migration verification passed."))
    extended = builder.build(restored, budget=budget, trace_id="live-anchor-extended")
    assert extended.metrics.anchor_status == "extended" and extended.anchor is not None
    assert extended.anchor.covered_turn_count > first.anchor.covered_turn_count
    print("[live] 主模型读取压缩后的历史并回答事实问题", flush=True)
    answer = model.delegate.chat(extended.messages, tools=None, max_tokens=512)
    assert "SQLite" in answer.text and "45" in answer.text, answer.text
    assert any(word in answer.text.lower() for word in ("fail", "not", "未", "失败", "没")), answer.text
    return {"live_anchor_ok": True, "provider": model.delegate.name, "model": model.delegate.model_name,
            "input_budget": budget.max_input_tokens, "created": first.metrics.model_dump(),
            "reused": reused.metrics.model_dump(), "extended": extended.metrics.model_dump(),
            "anchor": extended.anchor.model_dump(mode="json"), "rendered": render_anchor(extended.anchor),
            "model_calls": model.calls, "answer": answer.text, "answer_usage": answer.usage,
            "sqlite_restored": True, "source_unchanged": True, "recent_turns_raw": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="本次隔离数据与报告目录")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    db_path = args.output / "agent.db"
    if db_path.exists():
        parser.error("输出目录已经包含数据库，请使用新的目录")
    storage = RuntimeStorage.open(db_path)
    scope = MemoryScope(tenant_id="perception-live", user_id="synthetic")
    report = {"jev": {"url": provider.JEV_BASE_URL, "model": provider.JEV_MODEL,
              "configured_mode": provider.CONTEXT_ADMISSION_MODE, "key_configured": bool(provider.JEVMODEL_API_KEY)}}
    for name, probe in (("admission", probe_admission), ("anchor", probe_anchor)):
        try:
            report[name] = probe(storage, scope)
            print(f"[live] {name} 完成", flush=True)
        except Exception as exc:
            detail = str(exc)
            for key in (provider.JEVMODEL_API_KEY, provider.DEEPSEEK_API_KEY, provider.ANTHROPIC_API_KEY):
                if key:
                    detail = detail.replace(key, "[REDACTED]")
            report[name] = {"error_type": type(exc).__name__, "error": detail[:2000]}
            print(f"[live] {name} 失败: {type(exc).__name__}", flush=True)
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.output / "report.json"),
                      "jev_ok": report.get("admission", {}).get("live_judge_ok", False),
                      "anchor_ok": report.get("anchor", {}).get("live_anchor_ok", False)}, ensure_ascii=False), flush=True)
    if not (report.get("admission", {}).get("live_judge_ok") and report.get("anchor", {}).get("live_anchor_ok")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
