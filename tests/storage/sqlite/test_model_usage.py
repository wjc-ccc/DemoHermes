"""Real Loop call attempts must survive turn commit and restart."""
from agent.core.perception.perception_trace import PerceptionTrace
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.types.conversation import Message
from agent.types.memory.scope import MemoryScope
from agent.types.memory.source import TurnRecord
from agent.types.model import ModelResponse, ToolCallRequest
from agent.core.tools.registry import ToolRegistry
from tests.core.test_perception_context import _LongTool
from tests.loop_fakes import make_loop


def _commit(storage, session, result, trace, scope):
    turn_trace = trace.get(result.turn_id)
    storage.sessions.commit_turn(session, TurnRecord(
        turn_id=result.turn_id, session_id=session.session_id, scope=scope,
        user_message_id=session.messages[0].message_id,
        assistant_message_ids=tuple(m.message_id for m in session.messages if m.role == "assistant"),
        tool_call_ids=tuple(t.tool_call_id for t in session.tool_calls),
        started_at=1, finished_at=2, completed=result.completed, interrupted=False, error=result.error,
        perception_trace=turn_trace.model_dump(mode="json"),
        input_token_count=turn_trace.input_token_count, output_token_count=turn_trace.output_token_count,
    ), scope=scope)


def test_real_usage_from_all_iterations_is_committed_and_restored(tmp_path):
    class Model:
        name = "offline-provider"
        model_name = "fixture-model"
        def chat(self, messages, tools=None, **kwargs):
            if messages[-1]["role"] == "tool":
                return ModelResponse(text="done", finish_reason="stop",
                                     raw={"id":"response-2","model":"served-fixture","system_fingerprint":"fp-1"},
                                     usage={"input_tokens": 20, "output_tokens": 5, "cache_read_input_tokens": 7})
            return ModelResponse(tool_calls=[ToolCallRequest(id="call", name="long_output")],
                                 usage={"prompt_tokens": 10, "completion_tokens": 3})
    storage = RuntimeStorage.open(tmp_path / "agent.db")
    scope = MemoryScope(tenant_id="test", user_id="usage-user")
    session = storage.sessions.get_or_create("usage", scope=scope)
    tools = ToolRegistry()
    tools.register(_LongTool())
    trace = PerceptionTrace()
    result = make_loop(model=Model(), tools=tools, perception_trace=trace).run_turn(
        session, Message(role="user", content_text="check"))
    assert result.completed
    assert trace.get(result.turn_id).input_token_count == 37
    assert trace.get(result.turn_id).output_token_count == 8
    _commit(storage, session, result, trace, scope)
    reopened = RuntimeStorage.open(storage.db.path)
    restored = reopened.executions.get_record(result.turn_id)
    assert restored.input_token_count == 37
    assert restored.output_token_count == 8
    calls = [call for build in restored.perception_trace["builds"] for call in build["model_calls"]]
    assert len(calls) == 2
    assert all(call["provider"] == "offline-provider" and call["model"] == "fixture-model" for call in calls)
    assert calls[1]["usage"]["cache_read_input_tokens"] == 7
    assert calls[1]["finish_reason"] == "stop"
    assert calls[1]["response_id"] == "response-2"
    assert calls[1]["served_model"] == "served-fixture"
    with reopened.db.read() as conn:
        assert conn.execute("SELECT input_token_count,output_token_count FROM turns WHERE turn_id=?",
                            (result.turn_id,)).fetchone() == (37, 8)


def test_retry_attempt_is_recorded_and_unknown_usage_is_not_zero():
    class Model:
        count = 0
        def chat(self, messages, tools=None, **kwargs):
            self.count += 1
            if self.count == 1:
                raise RuntimeError("transient error")
            return ModelResponse(text="done", usage={"prompt_tokens": 10, "completion_tokens": 2})
    trace = PerceptionTrace()
    from agent.types.conversation import Session
    result = make_loop(model=Model(), perception_trace=trace, model_retries=1).run_turn(
        Session(), Message(role="user", content_text="retry"))
    turn = trace.get(result.turn_id)
    assert result.completed
    assert len(turn.builds[0].model_calls) == 2
    assert turn.builds[0].model_calls[0].error == "transient error"
    assert turn.builds[0].model_calls[1].error is None
    assert turn.input_token_count is None
    assert turn.output_token_count is None
