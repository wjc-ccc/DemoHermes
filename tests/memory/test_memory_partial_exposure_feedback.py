from __future__ import annotations

from types import SimpleNamespace

from agent.core.agent_loop import AgentLoop
from agent.core.memory.memory_feedback import MemoryFeedback
from agent.core.storage.sqlite.memory_retrieval import MemoryRetriever
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.tools.memory_tools import MemoryReadTool
from agent.types.conversation import Message, ToolCall
from agent.types.memory.feedback import MemoryUsageResultItem
from agent.types.memory.items import UserMemoryItem, content_sha256
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import TurnRecord
from agent.types.tools import ToolInvocationContext


class _CaptureJudge:
    id = "partial-read-judge"

    def __init__(self):
        self.requests = []

    def judge_usage(self, requests):
        self.requests.extend(requests)
        # 故意提出有益标签，验证无真实后续结果时运行时仍不会记为帮助。
        return [MemoryUsageResultItem(request.decision_id, "yes", "helpful", "test_claim",
                                      ("message:final",)) for request in requests]


def _partial_read_turn(tmp_path, *, with_final=True):
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(user_id="partial-reader")
    card = storage.memory.put(UserMemoryItem(
        item_id="partial-card", kind="reusable_experience", scope=scope,
        source_type="test", source_ref="original", source_authority="tool_verified",
        content="本次验证成功", summary="本次验证成功", content_hash=content_sha256("本次验证成功"),
        applicability="项目环境缺依赖", action="必须先执行卡片后半部分独有的修复命令",
        outcome="原实验测试通过", limitations="仅原实验环境", outcome_status="success",
        keywords=("项目环境", "修复命令", "测试通过"),
    ))
    session = storage.sessions.get_or_create("partial-reader", scope=scope)
    user = session.add_message(Message(message_id="user", role="user", content_text="读取这张卡片", created_at=1))
    request = session.add_message(Message(message_id="read-request", role="assistant", created_at=2,
        metadata={"tool_calls": [{"id": "protocol-read", "name": "memory_read", "arguments_raw": "{}"}]}))
    result = MemoryReadTool(MemoryRetriever(storage.db)).execute(
        {"reference": "card:partial-card", "limit": 5},
        context=ToolInvocationContext(memory_scope=scope),
    )
    assert result.ok and result.data["next_offset"] == 5 and card.action not in result.result_text
    tool = ToolCall(tool_call_id="read-fact", provider_tool_call_id="protocol-read", session_id=session.session_id,
        trajectory_id="partial-turn", tool_name="memory_read", status="success", result_data=result.data,
        result_text=result.result_text, readback_reference="card:partial-card", created_at=2.1, finished_at=2.2)
    session.tool_calls.append(tool)
    page = session.add_message(Message(message_id="read-page", role="tool", content_text=result.result_text,
        tool_call_ids=[tool.tool_call_id], created_at=2.2, metadata={"tool_call_id": "protocol-read"}))
    loop = SimpleNamespace(_memory_usage=storage.card_decisions,
                           model=SimpleNamespace(name="partial-main", model_name="v1"))
    loop._stage_memory_exposure = AgentLoop._stage_memory_exposure.__get__(loop)
    AgentLoop._record_manual_memory_exposure(
        loop, scope, session, user.message_id, "partial-turn", 2, 2.5,
        [{"role": "tool", "tool_call_id": "protocol-read", "content": result.result_text}], page.seq_id)
    assert storage.memory.get(card.item_id).used_count == 0
    with storage.db.read() as conn:
        assert conn.execute("SELECT count(*) FROM memory_card_decisions").fetchone()[0] == 0
    assistant_ids = [request.message_id]
    if with_final:
        final = session.add_message(Message(message_id="final", role="assistant", content_text="已读取卡片开头。", created_at=3))
        assistant_ids.append(final.message_id)
    turn = TurnRecord(turn_id="partial-turn", session_id=session.session_id, scope=scope,
        user_message_id=user.message_id, assistant_message_ids=tuple(assistant_ids),
        tool_call_ids=(tool.tool_call_id,), started_at=1, finished_at=4,
        completed=True, interrupted=False, error=None)
    storage.sessions.commit_turn(session, turn, scope=scope)
    assert storage.memory.get(card.item_id).used_count == 1
    with storage.db.read() as conn:
        identity = conn.execute("SELECT id FROM memory_card_decisions WHERE decision_type='usage'").fetchone()[0]
    return storage, card, result, identity


def test_partial_read_feedback_uses_visible_page_and_does_not_credit_hidden_action(tmp_path):
    storage, card, result, identity = _partial_read_turn(tmp_path)
    judge = _CaptureJudge()
    assert MemoryFeedback(storage, judge, "partial-main:v1").run_once()
    assert len(judge.requests) == 1
    visible = judge.requests[0].card
    assert visible["content"] == result.result_text
    assert "action" not in visible and card.action not in str(visible)
    decision = storage.card_decisions.get(identity)
    assert decision["effect"] == "unknown"
    assert storage.memory.get(card.item_id).helpful_count == 0


def test_partial_read_without_subsequent_evidence_finishes_unknown_without_judge(tmp_path):
    storage, card, _, identity = _partial_read_turn(tmp_path, with_final=False)
    judge = _CaptureJudge()
    assert MemoryFeedback(storage, judge, "partial-main:v1").run_once()
    assert judge.requests == []
    decision = storage.card_decisions.get(identity)
    assert decision["adopted"] == decision["effect"] == "unknown"
    assert storage.memory.get(card.item_id).helpful_count == 0
