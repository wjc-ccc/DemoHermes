"""应用装配与生命周期；core 负责本轮请求的内部能力。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from agent.channel.channel_frontier import FrontierChannel
from agent.core.agent_loop import AgentLoop
from agent.core.agent_loop_trace import AgentLoopTrace
from agent.core.tool_handler import ToolHandler
from agent.core.tool_dispatch import ToolDispatch
from agent.core.precision.percision_engine import PrecisionBuilder
from agent.core.precision.percision_anchor import ModelAnchorBuilder
from agent.types.precision.result import PrecisionConfig
from agent.core.environment_snapshot import EnvironmentSnapshotter
from agent.core.event_bus import EventBus
from agent.core.memory.memory_builder import MemoryBuilder
from agent.core.memory.memory_extractor import MemoryExtractor
from agent.core.precision.percision_trace import PrecisionTrace
from agent.gateway.gateway import Gateway
from agent.model.registry import ModelRegistry
from agent.skills.registry import SkillRegistry
from agent.skills.repository import SkillRepository
from agent.skills.selector import SkillSelector
from agent.skills.skill_manage_tool import SkillManageTool
from agent.skills.skill_tool import UseSkillTool
from agent.storage.sqlite.runtime_storage import RuntimeStorage
from agent.tools.calculator_tools import CalculatorTool
from agent.tools.registry import ToolRegistry
from agent.types.context import ContextBudget
from agent.types.runtime import RuntimeConfig, LoopConfig, RuntimeDescription
from dataclasses import replace

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(self, *, config, gateway, loop, models, tools, skills, storage,
                 memory, action, events, precision_trace, environment, tool_handler, tool_dispatch):
        self.config = config
        self.gateway = gateway
        self.loop = loop
        self.models = models
        self.tools = tools
        self.skills = skills
        self.storage = storage
        self.memory = memory
        self.action = action
        self.events = events
        self.precision_trace = precision_trace
        self.environment = environment
        self.tool_handler = tool_handler
        self.tool_dispatch = tool_dispatch
        self._started = False
        self._stopped = False

    def start(self) -> None:
        if self._started:
            return
        if self._stopped:
            raise RuntimeError("已停止的 Runtime 不能重新启动")
        # ACTION-HOOK-09：先查证遗留动作再接收请求；关闭模块时无此生命周期。
        if self.action is not None:
            self.action.recover()
        self.gateway.start()
        if self.memory is not None:
            self.memory.reviewer.start()
        self._started = True

    def stop(self) -> None:
        if self._stopped:
            return
        self.gateway.stop()
        if self.memory is not None:
            self.storage.review_scheduler.flush_all()
            self.memory.reviewer.stop(timeout=30)
            self.memory.reviewer.process_pending()
        self.tool_dispatch.stop()
        self._started = False
        self._stopped = True

    def describe(self) -> RuntimeDescription:
        return RuntimeDescription(
            memory=self.memory is not None, action=self.action is not None,
            storage="sqlite", database=str(self.storage.db.path),
            model=getattr(self.loop.model, "name", "unknown"),
            tools=tuple(self.tools.list_tools()), skills=tuple(self.skills.list_skills()),
        )

    def on_turn_committed(self, turn) -> None:
        if self.memory is None:
            return
        try:
            self.memory.reviewer.notify(turn)
        except Exception:
            logger.exception("Memory reviewer notify 失败 turn_id=%s", turn.turn_id)
        try:
            from agent.provider import SYSTEM_PROMPT_PATH

            self.environment.capture(
                session_id=turn.session_id, turn_id=turn.turn_id, cwd=os.getcwd(),
                model_name=None, system_prompt_path=SYSTEM_PROMPT_PATH,
                tool_schemas=self.tools.get_schemas(),
                skill_versions={name: self.skills.get(name).status for name in self.skills.list_skills()},
            )
        except Exception:
            logger.exception("env snapshot 失败 turn_id=%s", turn.turn_id)

    def on_session_end(self, session) -> None:
        if self.memory is not None:
            self.memory.reviewer.notify(session)


def build_runtime(config: RuntimeConfig | None = None, *, model_registry=None) -> AgentRuntime:
    from agent.provider import ROOT, build_admission_policy, build_context_judge

    config = config or RuntimeConfig()
    config = replace(config,
        manual_tokens=config.manual_tokens if config.manual_tokens is not None else int(os.getenv("AGENT_MEMORY_MANUAL_TOKENS", "1200")),
        experience_tokens=config.experience_tokens if config.experience_tokens is not None else int(os.getenv("AGENT_MEMORY_EXPERIENCE_TOKENS", "600")),
    )
    data_root = Path(config.data_root) if config.data_root is not None else ROOT / "data"
    models = model_registry if model_registry is not None else ModelRegistry.default()
    events, precision_trace = EventBus(), PrecisionTrace()
    trace = AgentLoopTrace(events, precision_trace)
    # 会话事实始终落库；Memory 开关只控制记忆能力的装配与生命周期。
    # ACTION-HOOK-08：可选模块在启动装配处注入；关闭时不导入 action。
    # action-only 只持久记账，不启用 Memory 工具与后台提炼。
    schema_dirs = ()
    if config.use_action:
        from agent.action.storage.migrations import schema_directory
        schema_dirs = (schema_directory(),)
    storage = RuntimeStorage.open(
        data_root / "agent.db", extractor_version=MemoryExtractor.version,
        extra_schema_dirs=schema_dirs,
    )
    # ---- 技能 + 工具（use_skill / skill_manage 是技能体系入口）----
    skills = SkillRegistry(skill_dirs=[ROOT / "agent" / "skills" / "builtin", data_root / "skills"],
                           selector=SkillSelector() if config.use_memory else None)
    skills_root = data_root / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    repository = SkillRepository(
        skills_root, registry=skills,
        audit_log=storage.audit if config.use_memory else None,
    )
    tools = ToolRegistry()
    tools.register(CalculatorTool())
    tools.register(UseSkillTool(skills))
    tools.register(SkillManageTool(repository))
    memory = None
    if config.use_memory:
        memory = MemoryBuilder(
            storage, models, root=data_root / "memory", project_root=ROOT, config=config,
        ).build()
        for tool in memory.tools:
            tools.register(tool)
    action = None
    if config.use_action:
        from agent.action.bootstrap import build_action_runtime
        action = build_action_runtime(storage, tools, config=config.action_config,
                                      event_bus=events, skill_repository=repository, trace=trace)
    budget = None
    if config.use_memory:
        budget = config.context_budget or ContextBudget(
            model_context_tokens=int(os.getenv("AGENT_MODEL_CONTEXT_TOKENS", "32768")),
            reserved_output_tokens=int(os.getenv("AGENT_RESERVED_OUTPUT_TOKENS", "4096")),
        )
    # ---- 对话核心 ----
    if action is not None:
        handler, dispatch = action.handler, action.dispatch
    else:
        dispatch = ToolDispatch(trace, registry=tools)
        handler = ToolHandler(dispatch, registry=tools)
    model = models.get()
    policy = build_admission_policy().model_copy(update={"readback": tools.get("memory_read") is not None})
    precision = PrecisionBuilder(
        trace=precision_trace, anchor_builder=ModelAnchorBuilder(model),
        context_judge=build_context_judge(), admission_policy=policy,
        config=PrecisionConfig(budget=budget),
    )
    loop = AgentLoop(model, precision=precision, tool_handler=handler,
                     skills=skills, trace=trace, config=LoopConfig())
    gateway = Gateway(
        loop=loop, store=storage.sessions,
        memory_context=memory.context if memory is not None else None,
        workers=config.workers, event_bus=events, action_runtime=action, precision_trace=precision_trace,
    )
    runtime = AgentRuntime(
        config=config, gateway=gateway, loop=loop, models=models, tools=tools,
        skills=skills, storage=storage, memory=memory, action=action,
        events=events, precision_trace=precision_trace, tool_handler=handler, tool_dispatch=dispatch,
        environment=EnvironmentSnapshotter(storage.executions) if config.use_memory else None,
    )
    gateway.describe_agent = runtime.describe
    gateway.on_turn_committed = runtime.on_turn_committed
    gateway.on_session_end = runtime.on_session_end
    gateway.register_channel(FrontierChannel())
    return runtime
