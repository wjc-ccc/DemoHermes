"""Anchor 来源通过真实 SQLite 保存、恢复并按原消息读回。"""
import re

from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.prompt.renderers import render_anchor_input
from agent.core.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.tools.memory_tools import MemoryReadTool
from agent.types.context import ANCHOR_SECTIONS, render_anchor
from agent.types.conversation import Message
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import TurnRecord
from agent.types.perception.result import PerceptionConfig
from agent.types.tools import ToolInvocationContext
from tests.anchor_fakes import DeterministicAnchorBuilder


class _PerMessage:
    def estimate_messages(self, messages):
        return len(messages)


def test_compressed_anchor_sources_survive_storage_and_read_back_exact_originals(tmp_path):
    # 使用与已知 ID 读取测试相同的正式 Store、Retriever 和权限上下文。
    storage = RuntimeStorage.open(tmp_path / "anchor-recall.db")
    scope = MemoryScope(user_id="anchor-owner")
    session = storage.sessions.get_or_create("anchor-history", scope=scope)
    session.add_message(Message(role="user", content_text="导出目录是 D:/reports，数量上限 37。"))
    session.add_message(Message(role="assistant", metadata={"tool_calls": [{
        "id": "historical-export", "name": "export_reports", "arguments_raw": "{}",
    }]}))
    session.add_message(Message(role="tool", content_text="导出成功。\n批次 ACTUAL-37，生成 37 项。",
                                metadata={"tool_call_id": "historical-export"}))
    session.add_message(Message(role="assistant", content_text="导出已完成。"))
    for index in range(2):
        session.add_message(Message(role="user", content_text=f"近期对话 {index}"))
        session.add_message(Message(role="assistant", content_text="收到"))
    session.add_message(Message(role="user", content_text="继续核对历史导出"))
    originals = {message.message_id: message.model_dump(mode="json") for message in session.messages}
    turn = TurnRecord(turn_id="historical-turn", session_id=session.session_id, scope=scope,
                      user_message_id=session.messages[0].message_id,
                      assistant_message_ids=tuple(message.message_id for message in session.messages
                                                  if message.role == "assistant"),
                      tool_call_ids=(), started_at=1, finished_at=2,
                      completed=True, interrupted=False, error=None)
    storage.sessions.commit_turn(session, turn, scope=scope)
    result = PerceptionBuilder(
        config=PerceptionConfig(compression_trigger_tokens=1),
        anchor_builder=DeterministicAnchorBuilder(),
    ).build(session, token_estimator=_PerMessage())
    assert result.anchor_updated and result.anchor is not None
    assert result.anchor.covered_turn_count == 1
    assert session.context_anchor is None
    assert {message.message_id: message.model_dump(mode="json") for message in session.messages} == originals
    assert not any(message.get("content") == session.messages[0].content_text
                   for message in result.messages if message.get("role") == "user")
    session.context_anchor = result.anchor
    storage.sessions.commit_turn(session, turn, scope=scope)

    # 重新打开数据库，避免误用内存中的 Anchor 或消息列表。
    reopened = RuntimeStorage.open(tmp_path / "anchor-recall.db")
    restored = reopened.sessions.get_session(session.session_id)
    assert restored.context_anchor == result.anchor
    assert {message.message_id: message.model_dump(mode="json") for message in restored.messages} == originals
    text = render_anchor(restored.context_anchor)
    references = set(re.findall(r"message:[0-9a-f-]{36}", text))
    expected = {f"message:{source}" for slot, _ in ANCHOR_SECTIONS
                for fact in getattr(restored.context_anchor, slot) for source in fact.source_message_ids}
    assert references == expected
    assert {originals[reference.partition(":")[2]]["role"] for reference in references} == {
        "user", "assistant", "tool",
    }
    reader = MemoryReadTool(MemoryRetriever(reopened.db))
    owner = ToolInvocationContext(memory_scope=scope)
    stranger = ToolInvocationContext(memory_scope=MemoryScope(user_id="another-user"))
    for reference in references:
        readback = reader.execute({"reference": reference}, context=owner)
        assert readback.ok, readback.error
        assert readback.readback_reference == reference
        assert readback.data["content"] == originals[reference.partition(":")[2]]["content_text"]
        assert not reader.execute({"reference": reference}, context=stranger).ok


def test_merge_input_groups_facts_by_slot_and_preserves_chronological_source_refs():
    empty = {slot: [] for slot, _ in ANCHOR_SECTIONS}
    text = render_anchor_input({
        "constraints": {"phase": "merge"},
        "existing_anchor": {**empty, "intent": [
            {"text": "旧约定", "source_ref": "f0", "source_message_ids": ["f0"], "status": "active"},
            {"text": "旧格式提示", "source_message_ids": [], "status": "active"},
        ]},
        "extraction_groups": [
            {**empty, "label": "older", "start_seq": 0, "end_seq": 15, "intent": [
                {"text": "旧区间约定", "source_ref": "f1", "source_message_ids": ["f1"], "status": "active"},
            ]},
            {**empty, "label": "newer", "start_seq": 16, "end_seq": 19, "intent": [
                {"text": "较新区间更正", "source_ref": "f2", "source_message_ids": ["f2"], "status": "active"},
            ]},
        ],
    })
    assert text.index("Fact f0") < text.index("Fact f1") < text.index("Fact f2") < text.index("## Slot changes")
    assert "Range 0-15" in text and "Range 16-19" in text
    assert 'source_message_ids: ["f2"]' in text
    assert "不得据此新增事实或编造来源" in text
