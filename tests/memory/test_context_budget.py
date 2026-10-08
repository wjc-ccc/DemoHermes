"""Slice 8: 整轮 ContextBudget——最终 messages 不超过输入预算，协议组不可拆（§4.3）。"""
from agent.core.context_budget import ContextBudget
from agent.core.agent_loop import AgentLoop
from agent.core.memory import (
    MarkdownBook,
    MemoryContextLoader,
    MemoryLayer,
    MemoryScope,
    MemorySnapshot,
    ensure_scope_dir,
)
from agent.core.types import Message, Session
from agent.model.baseStructure import BaseModel, ModelResponse
from tests.anchor_fakes import DeterministicAnchorBuilder

_SCOPE = MemoryScope(tenant_id="cli", user_id="u1")


class _FakeModel(BaseModel):
    name = "fake"

    def __init__(self, script: list[ModelResponse]):
        self._script = list(script)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self._script.pop(0)


class _FixedCostEstimator:
    """FakeTokenEstimator：每条 message 固定成本（§10.1-8 允许的外部 seam）。"""

    def __init__(self, per_message: int):
        self._per_message = per_message

    def estimate_messages(self, messages) -> int:
        return self._per_message * len(messages)


def _snapshot(tmp_path, content: str = "偏好：先给结论") -> MemorySnapshot:
    root = tmp_path / "memory"
    MarkdownBook(
        ensure_scope_dir(root, _SCOPE) / "MEMORY.md",
        layer=MemoryLayer.USER,
        type="long_term_fact",
        scope=_SCOPE,
        writable=True,
    ).replace(content)
    return MemoryContextLoader(root).freeze(_SCOPE, session_id="s1")


def _history_session(turns: int) -> Session:
    session = Session(session_id="s1")
    for i in range(1, turns + 1):
        session.add_message(Message(
            message_id=f"s1-user-{i}", role="user", content_text=f"第{i}轮问题"
        ))
        session.add_message(Message(
            message_id=f"s1-assistant-{i}",
            role="assistant",
            content_text=f"第{i}轮回答",
        ))
    return session


def _contents(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content")) for m in messages)


def _user_contents(messages: list[dict]) -> list[str]:
    return [str(m.get("content") or "") for m in messages if m.get("role") == "user"]


def test_long_session_request_stays_within_input_budget(tmp_path):
    """发给模型的最终 messages 必须 ≤ max_input_tokens；旧前缀由 Anchor 替代。"""
    snapshot = _snapshot(tmp_path)
    # 9 条消息 ×100 = 900；预算 1100-300=800 → Anchor + 最近两轮恰好 800
    budget = ContextBudget(model_context_tokens=1100, reserved_output_tokens=300)
    estimator = _FixedCostEstimator(per_message=100)

    model = _FakeModel([ModelResponse(text="回答")])
    loop = AgentLoop(model=model, anchor_builder=DeterministicAnchorBuilder())
    session = _history_session(turns=3)

    result = loop.run_turn(
        session, Message(role="user", content_text="最新问题"),
        memory_snapshot=snapshot,
        context_budget=budget, token_estimator=estimator,
    )

    assert result.completed, result.error
    sent = model.calls[0]
    assert estimator.estimate_messages(sent) <= budget.max_input_tokens
    text = _contents(sent)
    users = _user_contents(sent)
    assert "第1轮问题" not in users  # 最旧一轮不再作为原文 user
    assert "第1轮问题" in text  # Intent 槽保住
    assert "<context-anchor" in text
    assert "第3轮问题" in users
    assert "偏好：先给结论" in text  # Hot 记忆永远保留
    assert "最新问题" in text


def test_output_token_reservation_can_make_recent_window_impossible(tmp_path):
    """输出预留挤压到硬保留窗口以下时必须明确失败。"""
    snapshot = _snapshot(tmp_path)
    estimator = _FixedCostEstimator(per_message=100)

    success_model = _FakeModel([ModelResponse(text="回答")])
    success = AgentLoop(
        model=success_model, anchor_builder=DeterministicAnchorBuilder()
    ).run_turn(
        _history_session(turns=3), Message(role="user", content_text="最新问题"),
        memory_snapshot=snapshot,
        context_budget=ContextBudget(
            model_context_tokens=1100, reserved_output_tokens=300
        ),
        token_estimator=estimator,
    )
    assert success.completed

    failed_model = _FakeModel([ModelResponse(text="不应调用")])
    failed = AgentLoop(
        model=failed_model, anchor_builder=DeterministicAnchorBuilder()
    ).run_turn(
        _history_session(turns=3), Message(role="user", content_text="最新问题"),
        memory_snapshot=snapshot,
        context_budget=ContextBudget(
            model_context_tokens=1100, reserved_output_tokens=400
        ),
        token_estimator=estimator,
    )
    assert failed.error == "context_budget_exceeded"
    assert failed_model.calls == []


def test_latest_user_and_recent_two_turns_are_never_trimmed(tmp_path):
    """预算只够 P0 时也不能静默删除最近两个历史轮次。"""
    snapshot = _snapshot(tmp_path)
    # 永远保留 = system + hot memory + 最新 user = 3 条 → 预算恰好 300
    estimator = _FixedCostEstimator(per_message=100)

    model = _FakeModel([ModelResponse(text="回答")])
    session = _history_session(turns=3)
    result = AgentLoop(
        model=model, anchor_builder=DeterministicAnchorBuilder()
    ).run_turn(
        session, Message(role="user", content_text="最新问题"),
        memory_snapshot=snapshot,
        context_budget=ContextBudget(model_context_tokens=1000, reserved_output_tokens=700),
        token_estimator=estimator,
    )

    assert not result.completed
    assert result.error == "context_budget_exceeded"
    assert model.calls == []


def _tool_turn_session() -> Session:
    """第1轮普通对话 + 第2轮带两次工具调用的完整协议组。"""
    from agent.core.building_messages import build_assistant_message, build_tool_message
    from agent.core.types import ToolCall
    from agent.model.baseStructure import ToolCallRequest

    session = Session(session_id="s1")
    session.add_message(Message(role="user", content_text="第1轮问题"))
    session.add_message(Message(role="assistant", content_text="第1轮回答"))
    session.add_message(Message(role="user", content_text="查一下预算"))
    session.add_message(build_assistant_message("", [
        ToolCallRequest(id="call_1", name="calculator", arguments={"expression": "1+1"}),
        ToolCallRequest(id="call_2", name="calculator", arguments={"expression": "2+2"}),
    ]))
    session.add_message(build_tool_message(ToolCall(
        tool_call_id="call_1", tool_name="calculator",
        status="success", result_text="工具结果A",
    )))
    session.add_message(build_tool_message(ToolCall(
        tool_call_id="call_2", tool_name="calculator",
        status="success", result_text="工具结果B",
    )))
    session.add_message(Message(role="assistant", content_text="预算回答"))
    return session


def test_assistant_tool_call_and_tool_results_are_trimmed_as_one_group(tmp_path):
    """禁止拆 tool 协议组；仅有两轮时预算不足就整体失败。"""
    snapshot = _snapshot(tmp_path)
    estimator = _FixedCostEstimator(per_message=100)

    def run(max_input: int):
        model = _FakeModel([ModelResponse(text="回答")])
        result = AgentLoop(
            model=model, anchor_builder=DeterministicAnchorBuilder()
        ).run_turn(
            _tool_turn_session(), Message(role="user", content_text="最新问题"),
            memory_snapshot=snapshot,
            context_budget=ContextBudget(
                model_context_tokens=max_input + 300, reserved_output_tokens=300
            ),
            token_estimator=estimator,
        )
        return result, model.calls

    # 预算 1000：两轮原文都能进入，工具协议完整保留。
    result, calls = run(max_input=1000)
    assert result.completed, result.error
    sent = calls[0]
    tool_msgs = [m for m in sent if m["role"] == "tool"]
    asst_calls = [m for m in sent if m.get("tool_calls")]
    assert len(tool_msgs) == 2 and len(asst_calls) == 1
    assert "工具结果A" in _contents(sent) and "工具结果B" in _contents(sent)
    assert "第1轮问题" in _user_contents(sent)

    # 预算 900：硬保留窗口装不下时不拆协议，也不发送模型请求。
    result, calls = run(max_input=900)
    assert result.error == "context_budget_exceeded"
    assert calls == []


class _CharCostEstimator:
    """FakeTokenEstimator：按消息 content 字符数计成本（provider 缩短才能见效）。"""

    def estimate_messages(self, messages) -> int:
        return sum(len(str(m.get("content") or "")) for m in messages)


def test_experience_recall_is_present_in_context_with_history(tmp_path):
    """本轮冻结的经验与手册、历史一同进入模型输入。"""
    experience_block = "<memory-experiences>\n经验：先运行项目测试\n</memory-experiences>"
    snapshot = MemorySnapshot(
        scope=_SCOPE, session_id="s1", prompt="HOT记忆块", built_at=0.0,
        revisions=(), experience_prompt=experience_block,
    )
    estimator = _CharCostEstimator()
    model = _FakeModel([ModelResponse(text="回答")])
    result = AgentLoop(
        model=model, anchor_builder=DeterministicAnchorBuilder()
    ).run_turn(
        _history_session(turns=3), Message(role="user", content_text="最新问题"),
        memory_snapshot=snapshot,
        context_budget=ContextBudget(model_context_tokens=10**9, reserved_output_tokens=1),
        token_estimator=estimator,
    )

    assert result.completed, result.error
    sent = model.calls[0]
    text = _contents(sent)
    assert experience_block in text
    assert "第1轮问题" in text and "第2轮问题" in text and "第3轮问题" in text
    assert "HOT记忆块" in text


def test_same_messages_and_budget_produce_same_trimmed_context(tmp_path):
    """§4.3：相同输入与预算必须得到确定性相同的裁剪结果。"""
    snapshot = _snapshot(tmp_path)
    budget = ContextBudget(model_context_tokens=1100, reserved_output_tokens=300)
    estimator = _FixedCostEstimator(per_message=100)

    sent_runs = []
    for _ in range(2):
        model = _FakeModel([ModelResponse(text="回答")])
        result = AgentLoop(
            model=model, anchor_builder=DeterministicAnchorBuilder()
        ).run_turn(
            _history_session(turns=3), Message(role="user", content_text="最新问题"),
            memory_snapshot=snapshot,
            context_budget=budget, token_estimator=estimator,
        )
        assert result.completed, result.error
        sent_runs.append(model.calls[0])

    assert sent_runs[0] == sent_runs[1]


def test_impossible_required_context_returns_context_budget_exceeded_without_model_call(tmp_path):
    """§4.3 顺序 5：永远保留部分本身超限 → 明确失败，不发送已破坏协议的请求。"""
    snapshot = _snapshot(tmp_path)
    # 永远保留 = system + hot + 最新 user = 3 条 ×100 = 300；可用输入只给 200
    estimator = _FixedCostEstimator(per_message=100)

    model = _FakeModel([ModelResponse(text="不应被调用")])
    result = AgentLoop(
        model=model, anchor_builder=DeterministicAnchorBuilder()
    ).run_turn(
        _history_session(turns=3), Message(role="user", content_text="最新问题"),
        memory_snapshot=snapshot,
        context_budget=ContextBudget(model_context_tokens=500, reserved_output_tokens=300),
        token_estimator=estimator,
    )

    assert not result.completed
    assert result.error == "context_budget_exceeded"
    assert model.calls == []  # 未向模型发送任何请求
