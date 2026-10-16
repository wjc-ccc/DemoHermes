from __future__ import annotations
from agent.types.tools import ToolInvocationContext

import time
from datetime import datetime, timezone

from agent.core.storage.sqlite.markdown_documents import MarkdownDocuments


def test_manual_tool_rejects_model_supplied_path_and_target_user(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "manual-auth.db")
    documents = MarkdownDocuments(tmp_path / "manuals", storage.db)
    scope = MemoryScope(user_id="current-user")
    current = documents.read(scope)
    tool = MemoryEditTool(storage.memory, documents)
    for extra in ({"path": "POLICY.md"}, {"user_id": "another-user"}):
        result = tool.execute({"operation": "manual_replace", "content": "forbidden", "expected_hash": current.disk_hash, "reason": "test", **extra}, context=ToolInvocationContext(memory_scope=scope, source_message_id="m1"))
        assert not result.ok
    assert documents.read(scope).snapshot.content == ""
from agent.types.memory.scope import MemoryScope
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.types.memory.source import EvidenceRef, TurnRecord
from agent.types.conversation import Message, ToolCall
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.tools.memory_edit_tool import MemoryEditTool


def _seed_card(storage, scope):
    content = "以后用中文回答"
    return storage.memory.put(UserMemoryItem(
        item_id="card-1", kind="stable_preference",
        slot_key="stable_preference:assistant:response_language",
        source_type="memory_extraction", source_ref="old-message", scope=scope,
        summary=content, content=content, content_hash=content_sha256(content),
        source_authority="user_explicit",
        evidence=(EvidenceRef(source_type="message", source_id="old-message",
                              observed_at=datetime.fromtimestamp(1, timezone.utc)),),
    ))


def _run_edit(storage, tool, scope, index, user_text, edit_args, *, completed=True):
    session = storage.sessions.get_or_create("slot", scope=scope)
    user = session.add_message(Message(
        message_id=f"user-{index}", role="user", content_text=user_text,
        created_at=time.time() + index,
    ))
    result = tool.execute({**edit_args}, context=ToolInvocationContext(memory_scope=scope, source_message_id=user.message_id))
    assert result.ok, result.error
    call = ToolCall(
        tool_call_id=f"tool-{index}", session_id=session.session_id,
        trajectory_id=f"turn-{index}", tool_name="memory_edit",
        status="success", tool_call_result=result.data, pending_mutation=result.pending_mutation, result_text=result.result_text,
    )
    session.tool_calls.append(call)
    assistant = session.add_message(Message(
        message_id=f"assistant-{index}", role="assistant", content_text="收到"
    ))
    turn = TurnRecord(
        turn_id=f"turn-{index}", session_id=session.session_id, scope=scope,
        user_message_id=user.message_id, assistant_message_ids=(assistant.message_id,),
        tool_call_ids=(call.tool_call_id,), started_at=float(index),
        finished_at=float(index) + 0.1, completed=completed,
        interrupted=not completed, error=None,
    )
    storage.sessions.commit_turn(session, turn, scope=scope)


def test_update_and_delete_existing_card_commit_only_after_completed_turn(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    card = _seed_card(storage, scope)
    editor = MarkdownDocuments(tmp_path / "memory", storage.db)
    tool = MemoryEditTool(storage.memory, editor)
    _run_edit(storage, tool, scope, 1, "改成以后用英文回答", {
        "operation": "card_update", "item_id": card.item_id,
        "expected_revision": card.revision,
        "summary": "以后用英文回答", "content": "以后用英文回答",
    }, completed=False)
    assert storage.memory.get(card.item_id).content == "以后用中文回答"
    _run_edit(storage, tool, scope, 2, "改成以后用英文回答", {
        "operation": "card_update", "item_id": card.item_id,
        "expected_revision": card.revision,
        "summary": "以后用英文回答", "content": "以后用英文回答",
    })
    changed = storage.memory.get(card.item_id)
    assert changed.content == "以后用英文回答"
    assert changed.revision == 2
    assert editor.read(scope).snapshot.content == ""
    _run_edit(storage, tool, scope, 3, "忘掉回复语言偏好", {
        "operation": "card_delete", "item_id": card.item_id,
        "expected_revision": changed.revision,
    })
    assert storage.memory.search(scope, "", statuses=("active",)) == ()
    assert storage.memory.get(card.item_id).status.value == "deleted"


def test_correction_phrase_can_overwrite_explicit_profile(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    content = "用户是 Python 后端工程师。"
    card = storage.memory.put(UserMemoryItem(
        item_id="card-1", kind="profile", slot_key="profile:user:profession",
        source_type="memory_extraction", source_ref="old-message", scope=scope,
        summary=content, content=content, content_hash=content_sha256(content),
        source_authority="user_explicit",
        evidence=(EvidenceRef(source_type="message", source_id="old-message",
                              observed_at=datetime.fromtimestamp(1, timezone.utc)),),
    ))
    tool = MemoryEditTool(storage.memory, MarkdownDocuments(tmp_path / "memory", storage.db))
    _run_edit(storage, tool, scope, 1, "更正一下我的职业：我已经转岗，现在是 Go 后端工程师。", {
        "operation": "card_update", "item_id": f"card:{card.item_id}",
        "expected_revision": card.revision,
        "summary": "用户是 Go 后端工程师", "content": "用户现在是 Go 后端工程师。",
    })
    assert storage.memory.get(card.item_id).content == "用户现在是 Go 后端工程师。"


def test_fact_card_update_rejects_experience_fields_before_commit(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="cli", user_id="u1")
    card = _seed_card(storage, scope)
    tool = MemoryEditTool(storage.memory, MarkdownDocuments(tmp_path / "memory", storage.db))
    result = tool.execute({"operation": "card_update", "item_id": card.item_id, "expected_revision": card.revision, "summary": "以后用英文回答", "content": "以后用英文回答", "applicability": "长期有效", "outcome_status": "observed"}, context=ToolInvocationContext(memory_scope=scope, source_message_id="m"))
    assert not result.ok and "applicability" in result.error


def test_tool_cannot_create_card_or_change_other_users_card(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    owner = MemoryScope(tenant_id="cli", user_id="owner")
    other = MemoryScope(tenant_id="cli", user_id="other")
    card = _seed_card(storage, owner)
    tool = MemoryEditTool(storage.memory, MarkdownDocuments(tmp_path / "memory", storage.db))
    assert not tool.execute({"operation": "card_set"}, context=ToolInvocationContext(memory_scope=owner, source_message_id="m")).ok
    assert not tool.execute({"operation": "card_update", "item_id": card.item_id, "expected_revision": card.revision, "kind": "ongoing_fact", "summary": "以后用英文回答", "content": "以后用英文回答"}, context=ToolInvocationContext(memory_scope=owner, source_message_id="m")).ok
    assert not tool.execute({"operation": "card_update", "item_id": card.item_id, "expected_revision": card.revision, "summary": "以后用英文回答", "content": "以后用英文回答"}, context=ToolInvocationContext(memory_scope=other, source_message_id="m")).ok
    assert not tool.execute({"operation": "card_delete", "item_id": card.item_id}, context=ToolInvocationContext(memory_scope=owner, source_message_id="m")).ok

