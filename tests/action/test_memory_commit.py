from tests.loop_fakes import action_turn
import uuid
import time
import pytest
from datetime import datetime, timezone
from agent.core.action.bootstrap import build_action_runtime
from agent.core.action.storage.migrations import schema_directory
from agent.storage.sqlite.markdown_documents import MarkdownDocuments
from agent.types.memory.scope import MemoryScope
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.types.memory.source import EvidenceRef, TurnRecord
from agent.types.conversation import Message
from agent.types.model import ToolCallRequest
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.tools.memory_edit_tool import MemoryEditTool
from agent.tools.memory_tools import MemorySearchTool
from agent.tools.registry import ToolRegistry


def prepared_edit(tmp_path):
    storage = RuntimeStorage.open(tmp_path / "agent.db", extra_schema_dirs=(schema_directory(),))
    scope = MemoryScope(tenant_id="cli", user_id="u")
    text = "以后用中文回答"
    card = storage.memory.put(UserMemoryItem(item_id="card-1", kind="stable_preference",
        slot_key="stable_preference:assistant:response_language", source_type="memory_extraction", source_ref="old",
        scope=scope, summary=text, content=text, content_hash=content_sha256(text), source_authority="user_explicit",
        evidence=(EvidenceRef(source_type="message", source_id="old", observed_at=datetime.fromtimestamp(1, timezone.utc)),)))
    registry = ToolRegistry()
    registry.register(MemorySearchTool(MemoryRetriever(storage.db)))
    registry.register(MemoryEditTool(storage.memory, MarkdownDocuments(tmp_path / "memory", storage.db)))
    runtime = build_action_runtime(storage, registry)
    session = storage.sessions.get_or_create("slot", scope=scope)
    user = session.add_message(Message(role="user", content_text="改成以后用英文回答"))
    ctx = action_turn(runtime, session, user, turn_id="turn-edit", memory_scope=scope)
    runtime.handler.handle_batch((ToolCallRequest(id=uuid.uuid4().hex, name="memory_search", arguments={"query": "中文"}),), ctx)
    args = {"operation": "card_update", "item_id": card.item_id, "expected_revision": card.revision,
            "summary": "以后用英文回答", "content": "以后用英文回答"}
    call = runtime.handler.handle_batch((ToolCallRequest(id=uuid.uuid4().hex, name="memory_edit", arguments=args),), ctx)[0]
    assert call.ok, call.error
    session.tool_calls.append(call)
    turn = TurnRecord(turn_id=ctx.turn.turn_id, session_id=session.session_id, scope=scope,
                      user_message_id=user.message_id, tool_call_ids=(call.tool_call_id,),
                      assistant_message_ids=(), started_at=time.time(), finished_at=time.time(),
                      completed=True, interrupted=False, error=None)
    return storage, scope, runtime, session, turn, call, args


def test_card_success_is_confirmed_only_in_turn_transaction(tmp_path):
    storage, scope, runtime, session, turn, call, args = prepared_edit(tmp_path)
    try:
        entry_id = call.action_evidence.ledger_entry_id
        assert runtime.ledger.get(entry_id).phase == "prepared"
        assert storage.memory.get("card-1").content == "以后用中文回答"
        storage.sessions.commit_turn(session, turn, scope=scope)
        assert runtime.ledger.get(entry_id).effect_state == "succeeded"
        assert storage.memory.get("card-1").content == "以后用英文回答"
        ctx = action_turn(runtime, session, Message(), turn_id="retry", memory_scope=scope)
        replay = runtime.handler.handle_batch((ToolCallRequest(id=uuid.uuid4().hex, name="memory_edit", arguments=args),), ctx)[0]
        assert replay.ok and replay.pending_mutation is None
    finally:
        runtime.stop()


def test_interrupted_turn_cancels_deferred_effect(tmp_path):
    storage, scope, runtime, session, turn, call, _ = prepared_edit(tmp_path)
    try:
        storage.sessions.commit_turn(session, turn.model_copy(update={"completed": False, "interrupted": True}), scope=scope)
        assert runtime.ledger.get(call.action_evidence.ledger_entry_id).effect_state == "failed"
        assert storage.memory.get("card-1").content == "以后用中文回答"
    finally:
        runtime.stop()


def test_commit_rollback_does_not_leave_success_receipt(tmp_path, monkeypatch):
    storage, scope, runtime, session, turn, call, _ = prepared_edit(tmp_path)
    try:
        def broken(*args):
            raise RuntimeError("outbox failed")
        monkeypatch.setattr(storage.review_scheduler, "record_turn", broken)
        with pytest.raises(RuntimeError, match="outbox failed"):
            storage.sessions.commit_turn(session, turn, scope=scope)
        assert storage.memory.get("card-1").revision == 1
        assert runtime.ledger.get(call.action_evidence.ledger_entry_id).phase == "prepared"
    finally:
        runtime.stop()


def test_post_cannot_change_the_deferred_business_command(tmp_path):
    storage, scope, runtime, session, turn, call, _ = prepared_edit(tmp_path)
    try:
        call.pending_mutation = call.pending_mutation.model_copy(update={"content": "被改写的内容"})
        with pytest.raises(ValueError, match="deferred_command_changed_after_pre"):
            storage.sessions.commit_turn(session, turn, scope=scope)
        assert storage.memory.get("card-1").revision == 1
        assert runtime.ledger.get(call.action_evidence.ledger_entry_id).phase == "prepared"
    finally:
        runtime.stop()
