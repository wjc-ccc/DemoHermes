"""完整上下文快照：真实 Loop、失败迭代、冻结及 P3 回取。"""
import copy

from tests.loop_fakes import make_loop
from agent.core.event_bus import EventBus
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.perception.perception_trace import PerceptionTrace
from agent.types.context import ContextBudget
from agent.types.conversation import Message, Session
from agent.types.model import ModelResponse, ToolCallRequest
from agent.types.perception.result import PerceptionMetrics, PerceptionResult
from agent.types.tools import ToolCallResult
from agent.core.tools.base import Tool
from agent.core.tools.registry import ToolRegistry


class _LongTool(Tool):
    name = "long_output"
    description = "测试长工具输出"
    parameters = {"type": "object", "properties": {}}

    def execute(self, arguments, *, context):
        return ToolCallResult(ok=True, result_text="HEAD-" + "全文内容" * 3000 + "-TAIL")


class _TwoStepModel:
    def __init__(self):
        self.calls = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        if len(self.calls) == 1:
            return ModelResponse(tool_calls=[ToolCallRequest(id="call-1", name="long_output", arguments={})])
        return ModelResponse(text="完成")


def test_every_model_iteration_keeps_complete_messages_and_tools():
    model = _TwoStepModel()
    registry = ToolRegistry()
    registry.register(_LongTool())
    trace = PerceptionTrace()
    session = Session()
    result = make_loop(model=model, tools=registry, event_bus=EventBus(), perception_trace=trace).run_turn(
        session, Message(role="user", content_text="继续"))
    assert result.completed
    builds = trace.get(result.turn_id).builds
    assert [b.iteration for b in builds] == [1, 2]
    for build, (messages, tools) in zip(builds, model.calls):
        assert build.messages == messages
        assert build.tools == tools
    assert builds[1].messages[-1]["content"].endswith("-TAIL")
    assert len(builds[1].messages[-1]["content"]) > 10000
    session.messages[-1].content_text = "later change"
    assert trace.get(result.turn_id).builds[0].messages == model.calls[0][0]
    assert trace.snapshot().n == 1


def test_trace_owns_deep_snapshots_and_keeps_p3_readback():
    trace = PerceptionTrace()
    result = PerceptionResult(messages=[{"role": "user", "content": "original"}],
        metrics=PerceptionMetrics(trace_id="t"), exposed_refs=("tool:fact",))
    trace.record(result, iteration=1)
    tools = [{"function": {"name": "original"}}]
    trace.record_request("t", 1, tools)
    result.messages[0]["content"] = "changed"
    tools[0]["function"]["name"] = "changed"
    returned = trace.get("t")
    returned.builds[0].messages[0]["content"] = "external mutation"
    assert trace.get("t").builds[0].messages[0]["content"] == "original"
    assert trace.get("t").builds[0].tools[0]["function"]["name"] == "original"
    assert trace.record_memory_read("t", "tool:fact") == ("tool:fact",)
    assert trace.snapshot().p3_hit_rate == 1
    trace.record_p3_hit("t", "tool:fact")
    assert trace.snapshot().p3_hit_rate == 1


def test_failed_second_build_does_not_reuse_first_context():
    class CountMessages:
        def estimate_messages(self, messages):
            return len(messages)

    model = _TwoStepModel()
    registry = ToolRegistry()
    registry.register(_LongTool())
    trace = PerceptionTrace()
    result = make_loop(model=model, tools=registry, event_bus=EventBus(), perception_trace=trace,
        budget=ContextBudget(model_context_tokens=4, reserved_output_tokens=1),
        token_estimator=CountMessages()).run_turn(Session(), Message(role="user", content_text="now"))
    assert result.error == "context_budget_exceeded"
    assert len(model.calls) == 1
    builds = trace.get(result.turn_id).builds
    assert builds[0].messages is not None
    assert builds[1].messages is None
    assert builds[1].error == "context_budget_exceeded"


def test_failed_model_request_still_keeps_input():
    class FailingModel:
        def chat(self, messages, tools=None, **kwargs):
            raise RuntimeError("model unavailable")

    trace = PerceptionTrace()
    result = make_loop(model=FailingModel(), event_bus=EventBus(), perception_trace=trace,
        model_retries=0).run_turn(Session(), Message(role="user", content_text="retain this"))
    assert not result.completed
    assert trace.get(result.turn_id).builds[0].messages[-1]["content"] == "retain this"
