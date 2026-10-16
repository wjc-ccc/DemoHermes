"""测试用装配：在 Loop 的真实接口注入确定性模块。"""
from agent.core.agent_loop import AgentLoop
from agent.core.agent_loop_trace import AgentLoopTrace
from agent.core.event_bus import EventBus
from agent.core.perception.perception_anchor import ModelAnchorBuilder
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.perception.perception_trace import PerceptionTrace
from agent.core.tool_dispatch import ToolDispatch
from agent.core.tool_handler import ToolHandler
from agent.types.perception.admission import AdmissionPolicy
from agent.types.perception.result import PerceptionConfig
from agent.types.runtime import LoopConfig, TurnContext


def make_loop(model=None, *, tools=None, skills=None, event_bus=None,
              perception_trace=None, anchor_builder=None, anchor_model=None,
              action=None, context_judge=None, admission_policy=None,
              budget=None, token_estimator=None, selector=None,
              max_iterations=8, model_retries=2, agent_id="main",
              models=None, model_name=None):
    if model is None:
        if models is None:
            from agent.core.model.registry import ModelRegistry
            models = ModelRegistry.default()
        model = models.get(model_name)
    events = event_bus if event_bus is not None else EventBus()
    perception_trace = perception_trace if perception_trace is not None else PerceptionTrace()
    trace = AgentLoopTrace(events, perception_trace)
    if action is not None:
        handler = action.handler
        handler.dispatch.trace = trace
        handler.trace = trace
    else:
        handler = ToolHandler(ToolDispatch(trace, registry=tools), registry=tools)
    if skills is not None and selector is not None:
        skills.selector = selector
    policy = admission_policy or AdmissionPolicy()
    perception = PerceptionBuilder(
        trace=perception_trace,
        anchor_builder=anchor_builder if anchor_builder is not None else ModelAnchorBuilder(anchor_model or model),
        context_judge=context_judge,
        admission_policy=policy.model_copy(update={"readback": tools is not None and tools.get("memory_read") is not None}),
        config=PerceptionConfig(budget=budget, token_estimator=token_estimator),
    )
    return AgentLoop(model, perception=perception, tool_handler=handler,
                     skills=skills, trace=trace,
                     config=LoopConfig(max_iterations=max_iterations, model_retries=model_retries, agent_id=agent_id))


def action_turn(runtime, session, message, *, turn_id, session_key="", memory_scope=None,
                assembly_policy=None, agent_id="main"):
    return runtime.handler.begin_turn(TurnContext(
        session_id=session.session_id, author_id=session.author_id, channel=session.channel,
        session_key=session_key, turn_id=turn_id, source_message_id=message.message_id,
        memory_scope=memory_scope, assembly_policy=assembly_policy, agent_id=agent_id,
    ))
