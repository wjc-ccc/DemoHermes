"""Regression probes for the actual request, readback and turn lifecycle."""
import pytest

from agent.core.building_messages import build_tool_message
from agent.core.event_bus import EventBus
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.perception.perception_trace import PerceptionTrace
from agent.types.context import ContextBudget
from agent.types.conversation import Message, Session, ToolCall
from agent.types.model import ModelResponse, ToolCallRequest
from agent.types.perception.admission import AdmissionPolicy
from agent.types.perception.result import PerceptionMetrics, PerceptionResult
from agent.types.tools import ToolCallResult
from agent.core.tools.base import Tool
from agent.core.tools.registry import ToolRegistry
from tests.loop_fakes import make_loop


def test_empty_provider_response_retries_and_keeps_both_attempt_usages():
    class Model:
        calls=0
        def chat(self,messages,tools=None,**kwargs):
            self.calls+=1
            return ModelResponse(text="" if self.calls==1 else "实际回答",
                usage={"prompt_tokens":3,"completion_tokens":2})
    model=Model()
    loop=make_loop(model=model)
    result=loop.run_turn(Session(),Message(role="user",content_text="请回答这个问题"))
    assert result.completed and result.final_text=="实际回答"
    assert model.calls==2
    trace=loop.trace.perception.get(result.turn_id)
    assert trace.input_token_count==6 and trace.output_token_count==4
    assert trace.builds[0].model_calls[0].error=="empty_model_response"
from tests.core.test_perception_admission import (
    _build, _tool_turn, _body, _FakeJudge, _three_zone_session,
    _THREE_ZONE_NEEDS, _MemoryRead, _ReadbackModel,
)


def test_admission_recovery_uses_fact_id_and_keeps_provider_pairing(tmp_path):
    from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
    from agent.core.storage.sqlite.memory_retrieval import MemoryRetriever
    from agent.types.memory.scope import MemoryScope
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="test", user_id="synthetic")
    session = _three_zone_session()
    message = next(m for m in session.messages if m.role == "tool" and m.metadata["tool_call_id"] == "c_defer")
    call = ToolCall(tool_call_id="fact-defer", provider_tool_call_id="c_defer",
                    session_id=session.session_id, tool_name="bash", status="success",
                    result_text=message.content_text)
    session.tool_calls.append(call)
    message.tool_call_ids = [call.tool_call_id]
    from agent.types.memory.source import TurnRecord
    storage.sessions.commit_turn(session, TurnRecord(
        turn_id="history", session_id=session.session_id, scope=scope,
        user_message_id=session.messages[0].message_id,
        assistant_message_ids=tuple(m.message_id for m in session.messages if m.role == "assistant"),
        tool_call_ids=(call.tool_call_id,), started_at=1, finished_at=2,
        completed=True, interrupted=False, error=None,
    ), scope=scope)
    result = _build(session, judge=_FakeJudge(_THREE_ZONE_NEEDS), usage=0.75)
    verdict = result.verdicts[message.message_id]
    assert verdict.tool_call_id == "fact-defer"
    projected = next(m for m in result.messages if m.get("tool_call_id") == "c_defer")
    assert 'tool:fact-defer' in projected["content"]
    retriever = MemoryRetriever(storage.db)
    first_page = retriever.read(scope, "tool:fact-defer", limit=4000)
    second_page = retriever.read(scope, "tool:fact-defer", offset=4000, limit=4000)
    assert first_page.content + second_page.content == call.result_text


@pytest.mark.parametrize("name", ["read_file", "edit_file"])
def test_failed_same_resource_request_does_not_invalidate_old_success(name):
    session = Session()
    _tool_turn(session, "查看最初文件", name="read_file", arguments={"path": "src/data.py"},
               call_id="original", result=_body("original"))
    _tool_turn(session, "再次操作失败", name=name, arguments={"path": "src/data.py"},
               call_id="failed", result="access denied", error=True)
    for text in ("继续排查", "保留此前成功内容"):
        session.add_message(Message(role="user", content_text=text))
        session.add_message(Message(role="assistant", content_text="收到"))
    session.add_message(Message(role="user", content_text="核对最初读取的内容"))
    result = _build(session, judge=_FakeJudge(), usage=0.75)
    verdict = next(v for v in result.verdicts.values() if v.tool_call_id == "original")
    assert verdict.action == "keep"
    assert verdict.source != "rule"


@pytest.mark.parametrize("mode", ["shadow", "active"])
def test_readback_only_marks_results_that_were_actually_projected(mode):
    class FixedMessages:
        def estimate_messages(self, messages):
            return len(messages)
    session = _three_zone_session()
    first = _build(session, judge=_FakeJudge(_THREE_ZONE_NEEDS), usage=0.75)
    session.context_verdicts = first.verdicts
    session.messages.pop()
    registry = ToolRegistry()
    registry.register(_MemoryRead())
    result = make_loop(model=_ReadbackModel(), tools=registry,
                       admission_policy=AdmissionPolicy(mode=mode),
                       token_estimator=FixedMessages()).run_turn(session, Message(role="user", content_text="读回原文"))
    assert result.completed
    verdict = next(v for v in session.context_verdicts.values() if v.tool_call_id == "c_defer")
    assert not verdict.missed


def test_tool_schema_cost_prevents_oversized_model_request():
    class HugeTool(Tool):
        name = "huge_schema"
        description = "description " * 1000
        parameters = {"type": "object", "properties": {}}
        def execute(self, arguments, *, context):
            return ToolCallResult(ok=True, result_text="ok")
    class Model:
        calls = 0
        def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            return ModelResponse(text="ok")
    model = Model()
    tools = ToolRegistry()
    tools.register(HugeTool())
    result = make_loop(model=model, tools=tools,
                       budget=ContextBudget(model_context_tokens=1000, reserved_output_tokens=100)).run_turn(
                           Session(), Message(role="user", content_text="now"))
    assert result.error == "context_budget_exceeded"
    assert model.calls == 0


def test_broken_estimator_uses_the_same_conservative_cost_everywhere():
    class Broken:
        def estimate_messages(self, messages):
            raise ValueError("tokenizer unavailable")
    session = Session()
    session.add_message(Message(role="user", content_text="中文 Unicode 🧪"))
    expected = PerceptionBuilder().build(session)
    result = PerceptionBuilder().build(session, token_estimator=Broken())
    assert result.messages == expected.messages
    assert result.metrics.tokens_used == expected.metrics.tokens_used


def test_interleaved_uncommitted_turn_keeps_all_builds_when_window_is_one():
    trace = PerceptionTrace(window=1)
    def record(identity, iteration, **metrics):
        trace.record(PerceptionResult(messages=[{"role": "user", "content": identity}],
                     metrics=PerceptionMetrics(trace_id=identity, **metrics)), iteration=iteration)
    record("active", 1, admission_judged_count=3, budget_usage=0.9)
    record("other", 1)
    record("active", 2, budget_usage=0.2)
    turn = trace.get("active")
    assert [b.iteration for b in turn.builds] == [1, 2]
    assert turn.metrics.admission_judged_count == 3
    assert turn.metrics.budget_usage == 0.9


def test_missing_provider_id_is_normalized_before_assistant_is_recorded():
    class Model:
        def chat(self, messages, tools=None, **kwargs):
            if messages[-1]["role"] == "tool":
                return ModelResponse(text="done")
            return ModelResponse(tool_calls=[ToolCallRequest(name="memory_read", arguments={"reference": "tool:any"})])
    registry = ToolRegistry()
    registry.register(_MemoryRead())
    session = Session()
    assert make_loop(model=Model(), tools=registry).run_turn(session, Message(role="user", content_text="read")).completed
    assistant = next(m for m in session.messages if m.metadata.get("tool_calls"))
    tool = next(m for m in session.messages if m.role == "tool")
    assert assistant.metadata["tool_calls"][0]["id"]
    assert assistant.metadata["tool_calls"][0]["id"] == tool.metadata["tool_call_id"]


def test_reused_provider_id_has_distinct_persistent_fact_ids():
    class Model:
        def chat(self, messages, tools=None, **kwargs):
            if messages[-1]["role"] == "tool":
                return ModelResponse(text="done")
            return ModelResponse(tool_calls=[ToolCallRequest(id="shared", name="memory_read", arguments={"reference":"tool:any"})])
    registry = ToolRegistry()
    registry.register(_MemoryRead())
    loop = make_loop(model=Model(), tools=registry)
    session = Session()
    for text in ("first", "second"):
        assert loop.run_turn(session, Message(role="user", content_text=text)).completed
    assert len({call.tool_call_id for call in session.tool_calls}) == 2
    assert {call.provider_tool_call_id for call in session.tool_calls} == {"shared"}


def test_reused_protocol_id_does_not_change_which_request_a_result_belongs_to():
    session = Session()
    for identity, path, error in (("shared", "a.txt", False), ("shared", "b.txt", True), ("third", "a.txt", False)):
        _tool_turn(session, f"操作 {path}", name="read_file", arguments={"path":path},
                   call_id=identity, result=_body(path) if not error else "access denied", error=error)
    first = next(m for m in session.messages if m.role == "tool")
    first.tool_call_ids = ["first-fact"]
    for text in ("继续", "保留最新读取"):
        session.add_message(Message(role="user", content_text=text))
        session.add_message(Message(role="assistant", content_text="收到"))
    session.add_message(Message(role="user", content_text="核对 a.txt"))
    result = _build(session, judge=_FakeJudge(), usage=0.75)
    assert result.verdicts[first.message_id].source == "rule"
    assert result.verdicts[first.message_id].action == "defer"


def test_failed_turn_keeps_actual_tool_error_in_next_context():
    from agent.core.gateway.gateway import Gateway
    session = Session()
    _tool_turn(session, "迁移检查", name="bash", arguments={"command":"check"},
               call_id="failed", result="missing index", error=True)
    Gateway._mark_failed_turn_messages(session, session.messages[0])
    Gateway._append_gateway_error(session, "本轮失败，需要继续排查")
    session.add_message(Message(role="user", content_text="继续处理上次的错误"))
    result = PerceptionBuilder().build(session)
    assert any(m["role"] == "tool" and "missing index" in m["content"] for m in result.messages)
    assert not any("迁移检查 完成" in str(m.get("content")) for m in result.messages)


def test_parsed_arguments_survive_the_assistant_protocol_message():
    from agent.core.building_messages import build_assistant_message
    request = ToolCallRequest(id="request", name="read_file", arguments={"path":"actual.txt"})
    mapped = PerceptionBuilder._map_message(build_assistant_message("", [request]))
    import json
    assert json.loads(mapped["tool_calls"][0]["function"]["arguments"]) == {"path":"actual.txt"}


def test_real_request_output_limit_matches_reserved_budget():
    class Model:
        observed = None
        def chat(self, messages, tools=None, **kwargs):
            self.observed = kwargs.get("max_tokens")
            return ModelResponse(text="done")
    model = Model()
    result = make_loop(model=model, budget=ContextBudget(model_context_tokens=5000, reserved_output_tokens=25)).run_turn(
        Session(), Message(role="user", content_text="now"))
    assert result.completed
    assert model.observed == 25
