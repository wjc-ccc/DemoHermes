"""覆盖式 Anchor 通过 PrecisionBuilder interface 验证。"""
import json

import pytest

from agent.core.context_budget import ContextBudget, ContextBudgetExceededError
from agent.core.precision import (
    AnchorCompactionError,
    ModelAnchorBuilder,
    PrecisionBuilder,
)
from agent.core.precision.percision_anchor import AnchorBuildError, render_anchor
from agent.core.types import Anchor, Message, Session
from tests.anchor_fakes import DeterministicAnchorBuilder


class _PerMessage:
    """每条消息成本为一，便于精确控制窗口。"""

    def estimate_messages(self, messages) -> int:
        """返回消息条数。"""
        return len(messages)


def _history(turns: list[tuple[str, str]]) -> Session:
    """构造若干已完成历史轮次和一个最新 user。"""
    session = Session(session_id="s")
    for user, assistant in turns:
        session.add_message(Message(role="user", content_text=user))
        session.add_message(Message(role="assistant", content_text=assistant))
    session.add_message(Message(role="user", content_text="现在继续"))
    return session


def _build(session: Session, *, cap: int, builder=None):
    """用固定消息成本通过唯一 interface 构建上下文。"""
    return PrecisionBuilder(
        anchor_builder=builder or DeterministicAnchorBuilder()
    ).build(
        session,
        budget=ContextBudget(model_context_tokens=cap + 10, reserved_output_tokens=10),
        token_estimator=_PerMessage(),
        trace_id="compress",
    )


def test_anchor_replaces_covered_prefix_and_keeps_recent_two_turns_raw():
    """被 Anchor 覆盖的原文消失，最近两个完整轮次继续原样加载。"""
    result = _build(
        _history([
            ("第一轮问", "第一轮答"),
            ("第二轮问", "第二轮答"),
            ("第三轮问", "第三轮答"),
        ]),
        cap=8,
    )

    users = [
        message.get("content")
        for message in result.messages
        if message.get("role") == "user"
    ]
    assert result.anchor_updated is True
    assert result.anchor.covered_turn_count == 1
    assert "第一轮问" not in users
    assert "第二轮问" in users and "第三轮问" in users
    assert "第一轮问" in render_anchor(result.anchor)
    assert result.metrics.anchor_status == "created"
    assert result.metrics.compressed_count == 2


def test_valid_anchor_is_reused_below_threshold_without_builder_call():
    """已有 Anchor 每次都进入窗口，低于阈值时不重复压缩。"""
    session = _history([
        ("第一轮问", "第一轮答"),
        ("第二轮问", "第二轮答"),
        ("第三轮问", "第三轮答"),
    ])
    builder = DeterministicAnchorBuilder()
    first = _build(session, cap=8, builder=builder)
    session.context_anchor = first.context_anchor_payload

    second = _build(session, cap=100, builder=builder)
    users = [m.get("content") for m in second.messages if m.get("role") == "user"]

    assert len(builder.requests) == 1
    assert second.metrics.anchor_status == "reused"
    assert "第一轮问" not in users
    assert any("<context-anchor" in str(m.get("content")) for m in second.messages)
    assert any(
        record.item_name == "p2_anchor" and record.decision == "selected"
        for record in second.records
    )


def test_valid_legacy_dict_is_restored_as_global_anchor_type():
    """Session 恢复时把合法字典转成全局 Anchor 类型。"""
    session = _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")])
    created = _build(session, cap=8)

    restored = Session.model_validate({
        **session.model_dump(mode="json", exclude={"context_anchor"}),
        "context_anchor": created.context_anchor_payload,
    })

    assert isinstance(restored.context_anchor, Anchor)
    assert restored.context_anchor.session_id == restored.session_id


def test_invalid_anchor_payload_restores_all_raw_history():
    """非法字典不会阻止 Session 构造，也不会过滤原文。"""
    session = _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")])
    session.context_anchor = {"version": 2, "anchor_id": "broken"}

    result = _build(session, cap=100)
    users = [m.get("content") for m in result.messages if m.get("role") == "user"]

    assert "u1" in users
    assert result.anchor is None
    assert result.metrics.anchor_error_code == "anchor_payload_invalid"


def test_engine_returns_anchor_without_mutating_session():
    """PrecisionBuilder 仅返回新 Anchor，写回职责仍属于 AgentLoop。"""
    session = _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")])

    result = _build(session, cap=8)

    assert result.anchor_updated is True
    assert isinstance(result.anchor, Anchor)
    assert session.context_anchor is None


def test_anchor_extension_sends_only_uncovered_turns_to_builder():
    """扩展覆盖范围时不把已经覆盖的第一轮再次交给 Builder。"""
    session = _history([
        ("u1", "a1"),
        ("u2", "a2"),
        ("u3", "a3"),
    ])
    builder = DeterministicAnchorBuilder()
    first = _build(session, cap=8, builder=builder)
    session.context_anchor = first.context_anchor_payload
    session.add_message(Message(role="assistant", content_text="a4"))
    session.add_message(Message(role="user", content_text="u5"))
    session.add_message(Message(role="assistant", content_text="a5"))
    session.add_message(Message(role="user", content_text="u6"))

    second = _build(session, cap=8, builder=builder)
    second_input = [
        message["content"]
        for turn in builder.requests[1]["new_turns"]
        for message in turn["messages"]
    ]

    assert second.metrics.anchor_status == "extended"
    assert second.anchor.covered_turn_count > first.anchor.covered_turn_count
    assert "u1" not in second_input and "a1" not in second_input
    assert "u2" in second_input


def test_successful_action_is_kept_but_long_observation_is_not_rendered():
    """成功工具动作进入 changes，长 observation 不复制进主模型 Anchor。"""
    session = Session(session_id="s")
    session.add_message(Message(role="user", content_text="把登录改成验证码"))
    session.add_message(Message(
        role="assistant",
        content_text="",
        metadata={"tool_calls": [{
            "id": "c1", "name": "edit_file", "arguments_raw": "{}"
        }]},
    ))
    session.add_message(Message(
        role="tool",
        content_text="x" * 4000,
        metadata={"tool_call_id": "c1"},
    ))
    session.add_message(Message(
        role="assistant",
        content_text="排除了短信通道，下一步接邮箱",
    ))
    session.add_message(Message(role="user", content_text="第二轮问"))
    session.add_message(Message(role="assistant", content_text="第二轮答"))
    session.add_message(Message(role="user", content_text="第三轮问"))
    session.add_message(Message(role="assistant", content_text="第三轮答"))
    session.add_message(Message(role="user", content_text="现在继续"))

    result = _build(session, cap=10)
    text = render_anchor(result.anchor)

    assert "called edit_file" in text
    assert "x" * 100 not in text
    assert "把登录改成验证码" in text
    assert "排除了短信" in text
    assert "下一步" in text


def test_recent_two_turns_are_never_silently_dropped():
    """预算放不下最近两轮时明确报预算错误，不再删除最旧一轮。"""
    with pytest.raises(ContextBudgetExceededError):
        _build(_history([("u1", "a1"), ("u2", "a2")]), cap=4)


def test_builder_failure_retries_then_uses_raw_when_raw_fits():
    """两次 Builder 失败但原文仍在硬窗口内时安全退回原文。"""
    class _FailingBuilder:
        def __init__(self):
            self.calls = 0

        def __call__(self, payload):
            self.calls += 1
            raise AnchorBuildError("anchor_output_invalid", "bad json")

    builder = _FailingBuilder()
    session = _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")])
    result = _build(session, cap=8, builder=builder)

    assert builder.calls == 2
    assert result.metrics.anchor_status == "raw_fallback"
    assert result.metrics.anchor_error_code == "anchor_output_invalid"
    assert result.anchor_updated is False
    assert "u1" in [m.get("content") for m in result.messages]


def test_builder_failure_closes_when_raw_does_not_fit():
    """Builder 和原文都不可用时抛出专用错误，不静默丢消息。"""
    class _FailingBuilder:
        def __call__(self, payload):
            raise AnchorBuildError("anchor_model_error", "offline")

    with pytest.raises(AnchorCompactionError):
        _build(
            _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")]),
            cap=7,
            builder=_FailingBuilder(),
        )


def test_invalid_source_is_retried_with_validation_error():
    """伪造 message ID 的第一次输出被拒绝，第二次合法输出可通过。"""
    class _InvalidThenValid:
        def __init__(self):
            self.calls = []
            self.valid = DeterministicAnchorBuilder()

        def __call__(self, payload):
            self.calls.append(payload)
            if len(self.calls) == 1:
                return {
                    "intent": [{
                        "text": "伪造",
                        "source_message_ids": ["not-allowed"],
                        "status": "active",
                    }],
                    "changes": [],
                    "decisions": [],
                    "next_steps": [],
                    "excluded": [],
                }
            return self.valid(payload)

    builder = _InvalidThenValid()
    result = _build(
        _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")]),
        cap=8,
        builder=builder,
    )

    assert result.anchor_updated is True
    assert result.metrics.anchor_builder_attempts == 2
    assert "anchor_source_invalid" in (
        builder.calls[1]["constraints"]["validation_error_from_previous_attempt"]
    )


def test_digest_mismatch_restores_raw_history():
    """覆盖原文被修改后旧 Anchor 失效，构建恢复全量原文。"""
    session = _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")])
    first = _build(session, cap=8)
    session.context_anchor = first.context_anchor_payload
    session.messages[0].content_text = "u1 已修改"

    second = _build(session, cap=100)
    text = "\n".join(str(m.get("content")) for m in second.messages)

    assert "u1 已修改" in text
    assert "<context-anchor" not in text
    assert second.metrics.anchor_error_code == "anchor_digest_mismatch"


def test_legacy_anchor_never_filters_raw_before_v2_migration():
    """旧文本 Anchor 只能作为迁移提示，不能声明已经覆盖历史。"""
    session = _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")])
    session.summary = """<context-anchor>\nIntent:\n- legacy\nChanges:\nDecisions:\nNext steps:\nExcluded:\n</context-anchor>"""

    before = _build(session, cap=100)
    assert "u1" in [m.get("content") for m in before.messages]
    assert before.anchor is None

    migrated = _build(session, cap=8)
    assert migrated.anchor_updated is True
    assert migrated.anchor.version == 2


def test_agent_loop_persists_structured_anchor_on_session():
    """AgentLoop 只写 context_anchor，不再依赖 summary 字符串。"""
    from agent.core.agent_loop import AgentLoop
    from agent.model.baseStructure import BaseModel, ModelResponse

    class _Model(BaseModel):
        name = "fake"

        def chat(self, messages, tools=None, **kwargs):
            return ModelResponse(text="完成")

    session = _history([
        ("第1轮问题", "第1轮回答"),
        ("第2轮问题", "第2轮回答"),
        ("第3轮问题", "第3轮回答"),
    ])
    result = AgentLoop(
        model=_Model(), anchor_builder=DeterministicAnchorBuilder()
    ).run_turn(
        session,
        Message(role="user", content_text="最新问题"),
        context_budget=ContextBudget(model_context_tokens=18, reserved_output_tokens=10),
        token_estimator=_PerMessage(),
    )

    assert result.completed
    assert isinstance(session.context_anchor, Anchor)
    assert session.context_anchor.version == 2
    assert session.summary == ""


def test_model_anchor_builder_uses_no_tools_and_strict_prompt():
    """生产 Adapter 只做单次无工具 JSON 调用，并由 Engine 附加覆盖字段。"""
    class _AnchorModel:
        def __init__(self):
            self.calls = []

        def chat(self, messages, tools=None, **kwargs):
            self.calls.append((messages, tools))
            payload = json.loads(messages[1]["content"])
            source = payload["new_turns"][0]["messages"][0]["message_id"]
            body = {
                "intent": [{
                    "text": "第一轮目标",
                    "source_message_ids": [source],
                    "status": "active",
                }],
                "changes": [],
                "decisions": [],
                "next_steps": [],
                "excluded": [],
            }
            return type("Response", (), {"text": json.dumps(body, ensure_ascii=False)})()

    model = _AnchorModel()
    result = _build(
        _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")]),
        cap=8,
        builder=ModelAnchorBuilder(model),
    )

    assert result.anchor_updated is True
    assert model.calls[0][1] is None
    assert "不可信数据" in model.calls[0][0][0]["content"]


def test_agent_loop_maps_closed_compaction_failure_without_main_model_call():
    """失败关闭由 Loop 映射成稳定错误，且不会调用主模型。"""
    from agent.core.agent_loop import AgentLoop
    from agent.model.baseStructure import BaseModel, ModelResponse

    class _MainModel(BaseModel):
        name = "main"

        def __init__(self):
            self.calls = []

        def chat(self, messages, tools=None, **kwargs):
            self.calls.append(messages)
            return ModelResponse(text="不应调用")

    class _FailingBuilder:
        def __call__(self, payload):
            raise AnchorBuildError("anchor_model_error", "offline")

    model = _MainModel()
    result = AgentLoop(model=model, anchor_builder=_FailingBuilder()).run_turn(
        _history([("u1", "a1"), ("u2", "a2"), ("u3", "a3")]),
        Message(role="user", content_text="新的当前问题"),
        context_budget=ContextBudget(model_context_tokens=16, reserved_output_tokens=10),
        token_estimator=_PerMessage(),
    )

    assert result.error == "anchor_compaction_failed"
    assert model.calls == []
