"""经验统计必须影响真实召回，曝光和反馈必须走真实轮次提交链。"""
from __future__ import annotations

import time

from agent.core.agent_loop import AgentLoop
from agent.core.memory.config import experience_model_key
from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.core.memory.memory_embedding import experience_semantic_hash
from agent.core.memory.memory_feedback import MemoryFeedback
from agent.core.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.tools.base import Tool
from agent.core.tools.memory_tools import MemorySearchTool
from agent.core.tools.registry import ToolRegistry
from agent.types.conversation import Message
from agent.types.memory.feedback import MemoryUsageResultItem
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import EvidenceRef, TurnRecord
from agent.types.model import ModelResponse, ToolCallRequest
from agent.types.tools import ToolCallResult, ToolInvocationContext
from tests.loop_fakes import make_loop


def card(scope, identity, *, counts=0, metadata=None, enabled=True, action=None):
    text = f"{identity}：在Windows项目用虚拟环境运行pytest，观察到测试通过。"
    return UserMemoryItem(
        item_id=identity, kind="reusable_experience", scope=scope, content=text, summary=text,
        content_hash=content_sha256(text), source_type="test", source_ref="source-message",
        source_authority="tool_verified", keywords=("Windows", "pytest", "虚拟环境"),
        applicability="Windows项目执行pytest时", action=action or "使用项目虚拟环境运行pytest",
        outcome="12个测试通过", limitations="仅此项目的一次观察", outcome_status="success",
        evidence=(EvidenceRef(source_type="message", source_id="source-message"),),
        used_count=counts, judge_count=counts, effective_count=counts, helpful_count=counts,
        metadata=metadata or {}, recall_enabled=enabled,
    )


def scene(storage, scope, identity, parents, **kwargs):
    refs = [{"card_id": parent.item_id, "content_hash": parent.content_hash,
             "semantic_hash": experience_semantic_hash(parent)} for parent in parents]
    return storage.memory.put(card(scope, identity, metadata={"merged_from": refs}, **kwargs))


def test_maturity_ranks_relevant_cards_and_disabled_card_remains_searchable(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="user")
    storage.memory.put(card(scope, "new"))
    storage.memory.put(card(scope, "supported", counts=5))
    storage.memory.put(card(scope, "disabled", enabled=False))
    retriever = MemoryRetriever(storage.db)
    hits = retriever.recall(scope, "Windows pytest 虚拟环境", token_budget=1200)
    assert [hit.item_id for hit in hits] == ["supported", "new"]
    assert hits[0].score > hits[1].score
    assert "card:disabled" in {hit.reference for hit in retriever.search(scope, "pytest", source="card")}
    assert storage.memory.get("supported").maturity == 5 / 8
    assert storage.memory.get("supported").source_message_ids == ("source-message",)


def test_actual_scene_selection_suppresses_parents_and_changes_invalidate_scene(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="user")
    parents = [storage.memory.put(card(scope, f"parent-{i}", counts=5)) for i in range(3)]
    merged = scene(storage, scope, "scene", parents)
    retriever = MemoryRetriever(storage.db)
    assert [hit.item_id for hit in retriever.recall(scope, "Windows pytest")] == [merged.item_id]
    with storage.db.write() as conn:
        conn.execute("UPDATE memory_cards SET used_count=used_count+1 WHERE id=?", (parents[0].item_id,))
    assert retriever.recall(scope, "Windows pytest")[0].item_id == "scene"
    old = storage.memory.get(parents[0].item_id)
    storage.memory.put(old.model_copy(update={"action": "先检查新解释器的环境", "revision": old.revision+1}),
                       expected_revision=old.revision)
    assert "scene" not in {hit.item_id for hit in retriever.recall(scope, "Windows pytest")}
    assert storage.memory.get("scene") is not None


def test_failed_scene_trial_restores_parents_without_deleting_any_card(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="user")
    parents = [storage.memory.put(card(scope, f"p-{i}")) for i in range(3)]
    scene(storage, scope, "scene", parents)
    with storage.db.write() as conn:
        conn.execute("UPDATE memory_cards SET used_count=10,judge_count=10 WHERE id='scene'")
    hits = MemoryRetriever(storage.db).recall(scope, "pytest", token_budget=1200)
    assert {hit.item_id for hit in hits} == {p.item_id for p in parents}
    assert storage.memory.get("scene").used_count == 10


def test_oversized_scene_does_not_hide_parents_and_freeze_keeps_exact_hits(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="user")
    parents = [storage.memory.put(card(scope, f"p-{i}")) for i in range(3)]
    scene(storage, scope, "scene", parents, action="必须核对全部限制条件。" * 200)
    loader = MemoryContextLoader(tmp_path / "memory", experience_retriever=MemoryRetriever(storage.db))
    snapshot = loader.freeze(scope, session_id="session", query="Windows pytest")
    assert snapshot.experience_cards
    assert all(item.card_id != "scene" for item in snapshot.experience_cards)
    assert all(item.rendered_content in snapshot.experience_prompt for item in snapshot.experience_cards)


def test_source_message_reverse_lookup_is_scope_bound_and_uses_existing_evidence(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="user")
    storage.memory.put(card(scope, "own", enabled=False))
    storage.memory.put(card(MemoryScope(user_id="other"), "foreign"))
    tool = MemorySearchTool(MemoryRetriever(storage.db))
    result = tool.execute({"source_message_ids": ["source-message"]},
                          context=ToolInvocationContext(memory_scope=scope))
    assert result.ok
    assert [hit["item_id"] for hit in result.data["hits"]] == ["own"]
    assert "card_snapshot" not in result.data["hits"][0]


class VerifyTool(Tool):
    name = "verify_project"
    parameters = {"type": "object", "properties": {}}

    def execute(self, arguments, *, context):
        return ToolCallResult(ok=True, result_text="验证结果：12 passed，pytest退出状态0")


class Model:
    name = "fake"
    model_name = "experience"

    def chat(self, messages, tools=None, **kwargs):
        if any(message["role"] == "tool" for message in messages):
            return ModelResponse(text="已使用项目虚拟环境执行pytest，12个测试通过。")
        return ModelResponse(tool_calls=[ToolCallRequest(id="provider-id", name="verify_project", arguments={})])


class Judge:
    id = "offline"

    def judge_usage(self, requests):
        return [MemoryUsageResultItem(request.decision_id, "yes", "helpful", "实际执行与测试结果一致",
                   tuple(dict.fromkeys(entry["reference"] for entry in request.evidence))) for request in requests]


def test_two_model_iterations_count_one_exposure_and_committed_feedback_updates_card(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="user")
    storage.memory.put(card(scope, "experience"))
    session = storage.sessions.get_or_create("session", scope=scope)
    snapshot = MemoryContextLoader(tmp_path / "memory", experience_retriever=MemoryRetriever(storage.db)).freeze(
        scope, session_id=session.session_id, query="Windows pytest")
    registry = ToolRegistry()
    registry.register(VerifyTool())
    base = make_loop(model=Model(), tools=registry)
    loop = AgentLoop(base.model, perception=base.perception, tool_handler=base.tool_handler,
                     trace=base.trace, config=base.config, memory_usage=storage.card_decisions)
    user = Message(role="user", content_text="Windows项目运行pytest并验证")
    started = time.time()
    result = loop.run_turn(session, user, memory_snapshot=snapshot, memory_scope=scope)
    assert result.completed
    assert storage.memory.get("experience").used_count == 0
    assistants = [message for message in session.messages if message.role == "assistant"]
    assert [message.metadata["iteration"] for message in assistants] == [1, 2]
    tool = session.tool_calls[0]
    assert tool.metadata["assistant_message_id"] == assistants[0].message_id
    assert tool.tool_call_id != tool.provider_tool_call_id
    turn = TurnRecord(turn_id=result.turn_id, session_id=session.session_id, scope=scope,
        user_message_id=user.message_id, assistant_message_ids=tuple(m.message_id for m in assistants),
        tool_call_ids=(tool.tool_call_id,), started_at=started, finished_at=time.time(),
        completed=True, interrupted=False, error=None,
        perception_trace=loop.trace.perception.get(result.turn_id).model_dump(mode="json"))
    storage.sessions.commit_turn(session, turn, scope=scope)
    assert storage.memory.get("experience").used_count == 1
    jobs = storage.jobs.list(job_type="memory.feedback")
    assert len(jobs) == 1
    assert MemoryFeedback(storage, Judge(), experience_model_key(loop.model)).run_once()
    item = storage.memory.get("experience")
    assert (item.used_count, item.judge_count, item.effective_count, item.helpful_count) == (1, 1, 1, 1)
    assert item.revision == 1
    assert MemoryFeedback(storage, Judge(), experience_model_key(loop.model)).run_once() is False


def test_removed_experience_block_is_not_counted_as_exposure(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="user")
    storage.memory.put(card(scope, "experience"))
    session = storage.sessions.get_or_create("session", scope=scope)
    snapshot = MemoryContextLoader(tmp_path / "memory", experience_retriever=MemoryRetriever(storage.db)).freeze(
        scope, session_id=session.session_id, query="pytest")
    registry = ToolRegistry()
    registry.register(VerifyTool())
    base = make_loop(model=Model(), tools=registry)
    build = base.perception.build

    def without_experiences(*args, **kwargs):
        result = build(*args, **kwargs)
        return result.model_copy(update={"messages": [message for message in result.messages
            if message.get("content") != snapshot.experience_prompt]})

    base.perception.build = without_experiences
    loop = AgentLoop(base.model, perception=base.perception, tool_handler=base.tool_handler,
                     trace=base.trace, config=base.config, memory_usage=storage.card_decisions)
    assert loop.run_turn(session, Message(role="user", content_text="运行pytest"),
                         memory_snapshot=snapshot, memory_scope=scope).completed
    assert storage.memory.get("experience").used_count == 0
