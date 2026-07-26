"""Slice 8: 整轮 ContextBudget——最终 messages 不超过输入预算，协议组不可拆（§4.3）。"""
from Agent.core.context_budget import ContextBudget
from Agent.core.loop import AgentLoop
from Agent.core.types import Message, Session
from Agent.memory import (
    BuiltinMemoryMutation,
    MemoryContextSnapshot,
    MemoryManager,
    MemoryProvenance,
    MemoryScope,
)
from Agent.model.baseStructure import BaseModel, ModelResponse

_SCOPE = MemoryScope(kind="personal", key="cli:user:u1")
_PROVENANCE = MemoryProvenance(source="user_request")


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


def _add_profile(manager: MemoryManager, content: str) -> None:
    result = manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="profile", action="add",
            content=content, provenance=_PROVENANCE,
        )
    )
    assert result.ok, result.message


def _history_session(turns: int) -> Session:
    session = Session(session_id="s1")
    for i in range(1, turns + 1):
        session.add_message(Message(role="user", content_text=f"第{i}轮问题"))
        session.add_message(Message(role="assistant", content_text=f"第{i}轮回答"))
    return session


def _contents(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content")) for m in messages)


def test_long_session_request_stays_within_input_budget(tmp_path):
    """发给模型的最终 messages 必须 ≤ max_input_tokens；从最旧历史 turn 开始裁。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "偏好：先给结论")
    snapshot = manager.build_context(scope=_SCOPE, query="q", session_id="s1")
    # 9 条消息 ×100 = 900；预算 1000-300=700 → 裁掉最旧一轮（2 条）后恰好 700
    budget = ContextBudget(model_context_tokens=1000, reserved_output_tokens=300)
    estimator = _FixedCostEstimator(per_message=100)

    model = _FakeModel([ModelResponse(text="回答")])
    loop = AgentLoop(model=model)
    session = _history_session(turns=3)

    result = loop.run_turn(
        session, Message(role="user", content_text="最新问题"),
        memory_context=snapshot,
        context_budget=budget, token_estimator=estimator,
    )

    assert result.completed, result.error
    sent = model.calls[0]
    assert estimator.estimate_messages(sent) <= budget.max_input_tokens
    text = _contents(sent)
    assert "第1轮问题" not in text  # 最旧一轮被裁
    assert "第2轮问题" in text and "第3轮问题" in text
    assert "偏好：先给结论" in text  # Hot 记忆永远保留
    assert "最新问题" in text


def test_output_token_reservation_reduces_available_input_budget(tmp_path):
    """max_input_tokens 只能派生：输出预留越大，可用输入预算越小，裁得越多。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "偏好：先给结论")
    snapshot = manager.build_context(scope=_SCOPE, query="q", session_id="s1")
    estimator = _FixedCostEstimator(per_message=100)

    sent_sizes = {}
    for reserved in (300, 500):
        model = _FakeModel([ModelResponse(text="回答")])
        session = _history_session(turns=3)  # 9 条 ×100 = 900
        result = AgentLoop(model=model).run_turn(
            session, Message(role="user", content_text="最新问题"),
            memory_context=snapshot,
            context_budget=ContextBudget(
                model_context_tokens=1000, reserved_output_tokens=reserved
            ),
            token_estimator=estimator,
        )
        assert result.completed, result.error
        sent = model.calls[0]
        assert estimator.estimate_messages(sent) <= 1000 - reserved
        sent_sizes[reserved] = len(sent)

    # 预留 500 时可用输入 500，比预留 300（可用 700）裁掉更多历史
    assert sent_sizes[500] < sent_sizes[300]


def test_latest_user_message_is_never_trimmed(tmp_path):
    """预算只够永远保留部分时：历史全部裁掉，最新 user message 与 Hot 记忆仍在。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "偏好：先给结论")
    snapshot = manager.build_context(scope=_SCOPE, query="q", session_id="s1")
    # 永远保留 = system + hot memory + 最新 user = 3 条 → 预算恰好 300
    estimator = _FixedCostEstimator(per_message=100)

    model = _FakeModel([ModelResponse(text="回答")])
    session = _history_session(turns=3)
    result = AgentLoop(model=model).run_turn(
        session, Message(role="user", content_text="最新问题"),
        memory_context=snapshot,
        context_budget=ContextBudget(model_context_tokens=1000, reserved_output_tokens=700),
        token_estimator=estimator,
    )

    assert result.completed, result.error
    text = _contents(model.calls[0])
    assert "最新问题" in text
    assert "偏好：先给结论" in text
    assert "第1轮问题" not in text and "第3轮问题" not in text  # 历史全部裁掉


def _tool_turn_session() -> Session:
    """第1轮普通对话 + 第2轮带两次工具调用的完整协议组。"""
    from Agent.core.messages import build_assistant_message, build_tool_message
    from Agent.core.types import ToolCall
    from Agent.model.baseStructure import ToolCallRequest

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
    """§4.3：禁止单独删掉 tool result 或其 assistant tool-call——协议组同去同留。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "偏好：先给结论")
    snapshot = manager.build_context(scope=_SCOPE, query="q", session_id="s1")
    estimator = _FixedCostEstimator(per_message=100)

    def run(max_input: int) -> list[dict]:
        model = _FakeModel([ModelResponse(text="回答")])
        result = AgentLoop(model=model).run_turn(
            _tool_turn_session(), Message(role="user", content_text="最新问题"),
            memory_context=snapshot,
            context_budget=ContextBudget(
                model_context_tokens=max_input + 300, reserved_output_tokens=300
            ),
            token_estimator=estimator,
        )
        assert result.completed, result.error
        return model.calls[0]

    # 预算 900：只裁第1轮（2 条），工具协议组 5 条完整保留
    sent = run(max_input=900)
    tool_msgs = [m for m in sent if m["role"] == "tool"]
    asst_calls = [m for m in sent if m.get("tool_calls")]
    assert len(tool_msgs) == 2 and len(asst_calls) == 1
    assert "工具结果A" in _contents(sent) and "工具结果B" in _contents(sent)
    assert "第1轮问题" not in _contents(sent)

    # 预算 700：工具协议组整组裁掉，不剩半个协议
    sent = run(max_input=700)
    assert [m for m in sent if m["role"] == "tool"] == []
    assert [m for m in sent if m.get("tool_calls")] == []
    assert "工具结果A" not in _contents(sent)
    assert "最新问题" in _contents(sent)


_FIXED_NOON = 1784808000.0  # 2026-07-23 12:00:00 UTC


def test_optional_daily_is_removed_before_hot_memory(tmp_path):
    """§4.3 裁剪顺序 1：可选 Daily 最先移除；Hot profile/MEMORY 与历史都不先动。"""
    manager = MemoryManager(root=tmp_path / "memory", clock=lambda: _FIXED_NOON)
    _add_profile(manager, "偏好：先给结论")
    daily = manager.apply(
        BuiltinMemoryMutation(
            scope=_SCOPE, target="daily", action="add",
            content="今日流水：开了三个会", provenance=_PROVENANCE,
        )
    )
    assert daily.ok
    snapshot = manager.build_context(
        scope=_SCOPE, query="q", session_id="s1", include_today_daily=True
    )
    # 10 条 ×100 = 1000；预算 900 → 移除 daily（1 条）即达标，历史一条不裁
    estimator = _FixedCostEstimator(per_message=100)

    model = _FakeModel([ModelResponse(text="回答")])
    result = AgentLoop(model=model).run_turn(
        _history_session(turns=3), Message(role="user", content_text="最新问题"),
        memory_context=snapshot,
        context_budget=ContextBudget(model_context_tokens=1200, reserved_output_tokens=300),
        token_estimator=estimator,
    )

    assert result.completed, result.error
    text = _contents(model.calls[0])
    assert "今日流水：开了三个会" not in text  # 可选 Daily 最先被移除
    assert "偏好：先给结论" in text            # Hot 记忆保留
    assert "第1轮问题" in text and "第3轮问题" in text  # 历史尚未被裁


class _CharCostEstimator:
    """FakeTokenEstimator：按消息 content 字符数计成本（provider 缩短才能见效）。"""

    def estimate_messages(self, messages) -> int:
        return sum(len(str(m.get("content") or "")) for m in messages)


def test_provider_recall_is_shortened_before_old_turns_when_configured(tmp_path):
    """§4.3 裁剪顺序 2：配置了 provider 最小配额时，先缩短 recall 再动历史 turn。"""
    provider_block = (
        '<provider-memory name="fake">\n' + "长" * 2000 + "\n</provider-memory>"
    )
    snapshot = MemoryContextSnapshot(
        scope=_SCOPE, session_id="s1", prompt="HOT记忆块", built_at=0.0,
        revisions=(), provider_prompt=provider_block,
    )
    estimator = _CharCostEstimator()
    big_budget = ContextBudget(model_context_tokens=10**9, reserved_output_tokens=1)

    # 先用超大预算量出结构化注入的完整成本（公开 seam：启用预算但未触发裁剪）
    probe_model = _FakeModel([ModelResponse(text="探针")])
    AgentLoop(model=probe_model).run_turn(
        _history_session(turns=3), Message(role="user", content_text="最新问题"),
        memory_context=snapshot,
        context_budget=big_budget, token_estimator=estimator,
        provider_min_recall_chars=50,
    )
    full_cost = estimator.estimate_messages(probe_model.calls[0])

    # 只超 100 字符：缩短 provider（省 1950）即可达标；若先动历史，
    # 3 轮历史总共约 30 字符，裁光也不够 → 必然 exceeded 或丢历史
    model = _FakeModel([ModelResponse(text="回答")])
    result = AgentLoop(model=model).run_turn(
        _history_session(turns=3), Message(role="user", content_text="最新问题"),
        memory_context=snapshot,
        context_budget=ContextBudget(
            model_context_tokens=full_cost - 100 + 300, reserved_output_tokens=300
        ),
        token_estimator=estimator,
        provider_min_recall_chars=50,
    )

    assert result.completed, result.error
    sent = model.calls[0]
    provider_msgs = [m for m in sent if "<provider-memory" in str(m.get("content"))]
    assert provider_msgs == [
        {"role": "system", "content": '<provider-memory name="fake">\n' + "长" * 50 + "\n</provider-memory>"}
    ]
    text = _contents(sent)
    assert "第1轮问题" in text and "第2轮问题" in text and "第3轮问题" in text
    assert "HOT记忆块" in text


def test_same_messages_and_budget_produce_same_trimmed_context(tmp_path):
    """§4.3：相同输入与预算必须得到确定性相同的裁剪结果。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "偏好：先给结论")
    snapshot = manager.build_context(scope=_SCOPE, query="q", session_id="s1")
    budget = ContextBudget(model_context_tokens=1000, reserved_output_tokens=300)
    estimator = _FixedCostEstimator(per_message=100)

    sent_runs = []
    for _ in range(2):
        model = _FakeModel([ModelResponse(text="回答")])
        result = AgentLoop(model=model).run_turn(
            _history_session(turns=3), Message(role="user", content_text="最新问题"),
            memory_context=snapshot,
            context_budget=budget, token_estimator=estimator,
        )
        assert result.completed, result.error
        sent_runs.append(model.calls[0])

    assert sent_runs[0] == sent_runs[1]


def test_impossible_required_context_returns_context_budget_exceeded_without_model_call(tmp_path):
    """§4.3 顺序 5：永远保留部分本身超限 → 明确失败，不发送已破坏协议的请求。"""
    manager = MemoryManager(root=tmp_path / "memory")
    _add_profile(manager, "偏好：先给结论")
    snapshot = manager.build_context(scope=_SCOPE, query="q", session_id="s1")
    # 永远保留 = system + hot + 最新 user = 3 条 ×100 = 300；可用输入只给 200
    estimator = _FixedCostEstimator(per_message=100)

    model = _FakeModel([ModelResponse(text="不应被调用")])
    result = AgentLoop(model=model).run_turn(
        _history_session(turns=3), Message(role="user", content_text="最新问题"),
        memory_context=snapshot,
        context_budget=ContextBudget(model_context_tokens=500, reserved_output_tokens=300),
        token_estimator=estimator,
    )

    assert not result.completed
    assert result.error == "context_budget_exceeded"
    assert model.calls == []  # 未向模型发送任何请求
