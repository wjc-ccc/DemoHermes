from __future__ import annotations

import time

import pytest

from agent.core.agent_loop import AgentLoop
from agent.core.memory.config import experience_model_key
from agent.core.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.tools.memory_tools import MemoryReadTool, MemorySearchTool
from agent.core.tools.registry import ToolRegistry
from agent.types.conversation import Message
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.types.memory.scope import MemoryScope
from agent.types.memory.snapshot import ExperienceSnapshotItem, MemorySnapshot
from agent.types.memory.source import TurnRecord
from agent.types.model import ModelResponse, ToolCallRequest
from agent.types.tools import ToolInvocationContext
from tests.loop_fakes import make_loop


def _storage(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="manual-user")
    text = "在该项目缺少依赖时使用项目虚拟环境执行pytest，观察到测试通过。"
    card = UserMemoryItem(item_id="manual-card", kind="reusable_experience", scope=scope,
        source_type="test", source_ref="original", content=text, summary=text,
        content_hash=content_sha256(text), source_authority="model_inferred",
        keywords=("pytest", "依赖", "虚拟环境"), applicability="该项目解释器缺少依赖时",
        action="使用项目虚拟环境执行pytest", outcome="一次测试通过",
        limitations="仅这个项目的一次观察", outcome_status="success")
    storage.memory.put(card)
    return storage, scope, card


class _Model:
    name = "fake"
    model_name = "manual-model"

    def __init__(self, *, reads=1, search=False, fail=False):
        self.reads, self.search, self.fail, self.calls = reads, search, fail, 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls <= self.reads:
            name = "memory_search" if self.search else "memory_read"
            arguments = {"query": "card:manual-card"} if self.search else {"reference": "card:manual-card"}
            return ModelResponse(tool_calls=[ToolCallRequest(id=f"protocol-{self.calls}", name=name, arguments=arguments)])
        if self.fail:
            raise RuntimeError("offline simulated provider failure")
        return ModelResponse(text="已核对建议，本项目使用虚拟环境。")


def _run(storage, scope, *, model=None, transform=None, snapshot=None, max_iterations=5, commit=True):
    session = storage.sessions.get_or_create("manual-session", scope=scope)
    model = model or _Model()
    registry = ToolRegistry()
    retriever = MemoryRetriever(storage.db)
    registry.register(MemoryReadTool(retriever))
    registry.register(MemorySearchTool(retriever))
    base = make_loop(model=model, tools=registry, model_retries=0, max_iterations=max_iterations)
    if transform:
        build = base.perception.build
        def transformed(*args, **kwargs):
            result = build(*args, **kwargs)
            return result.model_copy(update={"messages": transform(result.messages)})
        base.perception.build = transformed
    loop = AgentLoop(model, perception=base.perception, tool_handler=base.tool_handler,
        trace=base.trace, config=base.config, memory_usage=storage.card_decisions)
    if snapshot is not None:
        snapshot = snapshot.model_copy(update={"session_id": session.session_id})
    user = Message(role="user", content_text="如何在此项目运行pytest？")
    result = loop.run_turn(session, user, memory_scope=scope, memory_snapshot=snapshot)
    turn = TurnRecord(turn_id=result.turn_id, session_id=session.session_id, scope=scope,
        user_message_id=user.message_id,
        assistant_message_ids=tuple(m.message_id for m in session.messages if m.role == "assistant"),
        tool_call_ids=tuple(t.tool_call_id for t in session.tool_calls), started_at=user.created_at,
        finished_at=time.time(), completed=result.completed, interrupted=result.interrupted, error=result.error,
        perception_trace=base.trace.perception.get(result.turn_id).model_dump(mode="json"))
    if commit:
        storage.sessions.commit_turn(session, turn, scope=scope)
    return result, session, turn, model


def test_card_read_binds_revision_in_data_without_exposing_internal_snapshot(tmp_path):
    storage, scope, _ = _storage(tmp_path)
    retriever = MemoryRetriever(storage.db)
    page = retriever.read(scope, "card:manual-card", offset=3, limit=40)
    assert page.card_revision == 1 and page.offset == 3
    result = MemoryReadTool(retriever).execute({"reference": "card:manual-card"},
        context=ToolInvocationContext(memory_scope=scope))
    assert result.data["card_revision"] == 1
    assert "card_snapshot" not in result.data and "card_revision" not in result.result_text


def test_manual_card_read_counts_only_when_sent_in_next_model_request(tmp_path):
    storage, scope, _ = _storage(tmp_path)
    result, session, turn, model = _run(storage, scope)
    assert result.completed and storage.memory.get("manual-card").used_count == 1
    row = storage.card_decisions.pending_usage(turn_id=turn.turn_id, model_key=experience_model_key(model))[0]
    context = row["result_json"]["context"]
    assert context["first_iteration"] == 2 and context["after_seq"] == 2
    assert context["exposure_source"] == "memory_read"
    assert context["tool_call_id"] == session.tool_calls[0].tool_call_id
    assert context["card_content"] == session.tool_calls[0].result_text


def test_exposure_is_only_staged_until_turn_commit(tmp_path):
    storage, scope, _ = _storage(tmp_path)
    result, session, turn, model = _run(storage, scope, commit=False)
    assert result.completed and storage.memory.get("manual-card").used_count == 0
    source = next(message for message in session.messages if message.message_id == turn.user_message_id)
    assert len(source.metadata["memory_exposures"]) == 1
    assert storage.executions.get_record(turn.turn_id) is None
    with storage.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_card_decisions").fetchone()[0] == 0
    storage.sessions.commit_turn(session, turn, scope=scope)
    assert storage.memory.get("manual-card").used_count == 1
    assert len(storage.card_decisions.pending_usage(turn_id=turn.turn_id,
        model_key=experience_model_key(model))) == 1


def test_commit_failure_rolls_back_exposure_counts_facts_and_feedback_job(tmp_path, monkeypatch):
    import agent.core.storage.sqlite.session_store as session_module
    storage, scope, _ = _storage(tmp_path)
    _, session, turn, _ = _run(storage, scope, commit=False)
    enqueue = session_module.enqueue_job
    def fail_feedback(conn, **kwargs):
        if kwargs["job_type"] == "memory.feedback":
            raise RuntimeError("simulated outbox failure")
        return enqueue(conn, **kwargs)
    monkeypatch.setattr(session_module, "enqueue_job", fail_feedback)
    with pytest.raises(RuntimeError, match="simulated outbox failure"):
        storage.sessions.commit_turn(session, turn, scope=scope)
    assert storage.memory.get("manual-card").used_count == 0
    assert storage.executions.get_record(turn.turn_id) is None
    with storage.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_card_decisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE turn_id=?", (turn.turn_id,)).fetchone()[0] == 0
    assert not storage.jobs.list(job_type="memory.feedback")
    monkeypatch.setattr(session_module, "enqueue_job", enqueue)
    storage.sessions.commit_turn(session, turn, scope=scope)
    storage.sessions.commit_turn(session, turn, scope=scope)
    assert storage.memory.get("manual-card").used_count == 1
    with storage.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_card_decisions").fetchone()[0] == 1
    assert len(storage.jobs.list(job_type="memory.feedback")) == 1


@pytest.mark.parametrize("mode", ["trim", "wrong_protocol", "search", "failure", "no_next_request"])
def test_unseen_trimmed_search_or_failed_request_does_not_count(tmp_path, mode):
    storage, scope, _ = _storage(tmp_path)
    transform = None
    if mode in {"trim", "wrong_protocol"}:
        def transform(messages):
            return [{**m, **({"content": "[已省略]"} if mode == "trim" else {"tool_call_id": "different"})}
                    if m.get("role") == "tool" else m for m in messages]
    _run(storage, scope, model=_Model(search=mode == "search", fail=mode == "failure"),
         transform=transform, max_iterations=1 if mode == "no_next_request" else 5)
    assert storage.memory.get("manual-card").used_count == 0


def test_repeated_manual_reads_and_auto_recall_share_turn_deduplication(tmp_path):
    storage, scope, card = _storage(tmp_path)
    text = "自动展示的项目虚拟环境建议"
    snapshot = MemorySnapshot(scope=scope, session_id="pending", prompt="记忆说明", built_at=time.time(),
        revisions=(), experience_prompt=text,
        experience_cards=(ExperienceSnapshotItem(card_id=card.item_id, revision=card.revision,
            content_hash=card.content_hash, card_content=card.model_dump(mode="json"), rendered_content=text),))
    _, _, turn, model = _run(storage, scope, model=_Model(reads=2), snapshot=snapshot)
    assert storage.memory.get(card.item_id).used_count == 1
    rows = storage.card_decisions.pending_usage(turn_id=turn.turn_id, model_key=experience_model_key(model))
    assert len(rows) == 1 and rows[0]["result_json"]["context"]["first_iteration"] == 1
