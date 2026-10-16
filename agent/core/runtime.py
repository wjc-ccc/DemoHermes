"""应用装配与生命周期。模型、渠道、网关、技能、工具和存储都在 core 内装配。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from agent.core.channel.channel_frontier import FrontierChannel
from agent.core.agent_loop import AgentLoop
from agent.core.agent_loop_trace import AgentLoopTrace
from agent.core.tool_handler import ToolHandler
from agent.core.tool_dispatch import ToolDispatch
from agent.core.perception.perception_engine import PerceptionBuilder
from agent.core.perception.perception_anchor import ModelAnchorBuilder
from agent.types.perception.result import PerceptionConfig
from agent.core.environment_snapshot import EnvironmentSnapshotter
from agent.core.event_bus import EventBus
from agent.core.cron.jobs import JobStore
from agent.core.cron.mail import NeteaseMailbox
from agent.core.cron.schedule_tool import ScheduleTool
from agent.core.cron.scheduler import CronScheduler
from agent.core.memory.memory_builder import MemoryBuilder
from agent.core.memory.memory_extractor import MemoryExtractor
from agent.core.perception.perception_trace import PerceptionTrace
from agent.core.gateway.gateway import Gateway
from agent.core.model.registry import ModelRegistry
from agent.core.skills.registry import SkillRegistry
from agent.core.skills.repository import SkillRepository
from agent.core.skills.selector import SkillSelector
from agent.core.skills.skill_manage_tool import SkillManageTool
from agent.core.skills.skill_tool import UseSkillTool
from agent.core.storage.sqlite.runtime_storage import RuntimeStorage
from agent.core.tools.bash_tools import BashTool
from agent.core.tools.calculator_tools import CalculatorTool
from agent.core.tools.file_tools import EditFileTool, GlobTool, ReadFileTool, WriteFileTool
from agent.core.tools.grep_tool import GrepTool
from agent.core.tools.local_support import stop_background
from agent.core.tools.registry import ToolRegistry
from agent.types.context import ContextBudget
from agent.types.runtime import RuntimeConfig, RuntimeDescription
from dataclasses import replace

logger = logging.getLogger(__name__)

### 负责所有后台进程 或者 线程的启动 （memory reviewer or gateway or cron 管理 or 邮件监督） 
class AgentRuntime:
    def __init__(self, *, config, gateway, loop, models, tools, skills, storage,
                 memory, action, events, perception_trace, environment, tool_handler, tool_dispatch,
                 scheduler=None):
        self.config = config
        self.gateway = gateway
        self.scheduler = scheduler
        self.loop = loop
        self.models = models
        self.tools = tools
        self.skills = skills
        self.storage = storage
        self.memory = memory
        self.action = action
        self.events = events
        self.perception_trace = perception_trace
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
        ## 启动 channel 启动
        if self.action is not None:
            self.action.recover()
        self.gateway.start()
        if self.scheduler is not None:
            self.scheduler.start()
        if self.memory is not None:
            self.memory.reviewer.start()
        self._started = True

    def stop(self) -> None:
        if self._stopped:
            return
        if self.scheduler is not None:
            self.scheduler.stop()
        self.gateway.stop()
        if self.memory is not None:
            self.storage.review_scheduler.flush_all()
            self.memory.reviewer.stop(timeout=30)
            self.memory.reviewer.process_pending()
        stop_background()
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
            from agent.core.provider import SYSTEM_PROMPT_PATH

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
    from agent.core.config import LoopConfig
    from agent.core.memory.config import MemoryContextConfig, MemoryScheduleConfig
    from agent.core.provider import ROOT, build_admission_policy, build_context_judge

    config = config or RuntimeConfig()
    context_budget_cfg = config.memory_context_config or MemoryContextConfig()
    if config.manual_tokens is not None:
        manual_tokens = config.manual_tokens
    elif config.memory_context_config is not None:
        manual_tokens = config.memory_context_config.manual_tokens
    else:
        manual_tokens = int(os.getenv("AGENT_MEMORY_MANUAL_TOKENS", str(context_budget_cfg.manual_tokens)))
    if config.experience_tokens is not None:
        experience_tokens = config.experience_tokens
    elif config.memory_context_config is not None:
        experience_tokens = config.memory_context_config.experience_tokens
    else:
        experience_tokens = int(os.getenv("AGENT_MEMORY_EXPERIENCE_TOKENS", str(context_budget_cfg.experience_tokens)))
    config = replace(config, manual_tokens=manual_tokens, experience_tokens=experience_tokens)
    data_root = Path(config.data_root) if config.data_root is not None else ROOT / "data"
    models = model_registry if model_registry is not None else ModelRegistry.default()
    events, perception_trace = EventBus(), PerceptionTrace()
    trace = AgentLoopTrace(events, perception_trace)
    # 会话事实始终落库；Memory 开关只控制记忆能力的装配与生命周期。
    # ACTION-HOOK-08：可选模块在启动装配处注入；关闭时不导入 action。
    # action-only 只持久记账，不启用 Memory 工具与后台提炼。
    schema_dirs = ()
    if config.use_action:
        from agent.core.action.storage.migrations import schema_directory
        schema_dirs = (schema_directory(),)
    schedule = config.memory_schedule_config or MemoryScheduleConfig()
    storage = RuntimeStorage.open(
        data_root / "agent.db", extractor_version=MemoryExtractor.version,
        extra_schema_dirs=schema_dirs, review_cadence=schedule.is_boundary,
        experience_config=config.memory_experience_config,
    )

    # ---- 技能 + 工具（use_skill / skill_manage 是技能体系入口）----
    skills = SkillRegistry(skill_dirs=[Path(__file__).resolve().parent / "skills" / "builtin", data_root / "skills"],
                           selector=SkillSelector() if config.use_memory else None)
    skills_root = data_root / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    repository = SkillRepository(
        skills_root, registry=skills,
        audit_log=storage.audit if config.use_memory else None,
    )


    tools = ToolRegistry()
    tools.register(CalculatorTool())
    tools.register(ReadFileTool())
    tools.register(WriteFileTool())
    tools.register(EditFileTool())
    tools.register(GlobTool())
    tools.register(GrepTool())
    tools.register(BashTool())
    tools.register(UseSkillTool(skills))
    cron_jobs = JobStore(data_root / "users")
    tools.register(ScheduleTool(cron_jobs))
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
        from agent.core.action.bootstrap import build_action_runtime
        action = build_action_runtime(storage, tools, config=config.action_config,
                                      event_bus=events, skill_repository=repository, trace=trace)
    budget = config.context_budget or ContextBudget(
        model_context_tokens=int(os.getenv("AGENT_MODEL_CONTEXT_TOKENS", "500000")),
        reserved_output_tokens=int(os.getenv("AGENT_RESERVED_OUTPUT_TOKENS", "100000")),
    )
    # ---- 对话核心 ----
    if action is not None:
        handler, dispatch = action.handler, action.dispatch
    else:
        dispatch = ToolDispatch(trace, registry=tools)
        handler = ToolHandler(dispatch, registry=tools)
    model = models.get()
    admission = (config.admission_config or build_admission_policy()).model_copy(
        update={"readback": tools.get("memory_read") is not None},
    )
    judge = config.judge_config
    perception_config = config.perception_config or PerceptionConfig()

    perception = PerceptionBuilder(
        trace=perception_trace, 
        anchor_builder=ModelAnchorBuilder(model, provider_max_output_tokens=perception_config.anchor_provider_output_limit,),
        context_judge=build_context_judge(
            timeout=None if judge is None else judge.timeout_seconds,
            batch_size=None if judge is None else judge.batch_size,
            max_workers=None if judge is None else judge.max_workers,
            min_kind_probability=admission.min_kind_probability,
        ),
        admission_policy=admission,
        config=replace(perception_config, budget=budget),
        token_estimate=config.token_estimate_config,
    )
    ## agentloop 负责感知模块 工具模块  skill  
    loop = AgentLoop(model, perception=perception, tool_handler=handler,
                     skills=skills, trace=trace, config=config.loop_config or LoopConfig(),
                     memory_usage=(storage.card_decisions if memory is not None
                                   and (config.memory_experience_config is None
                                        or config.memory_experience_config.enabled) else None))
    
    ## agent loop调用  用户 schema获取  channel信息校验  message bus管理 store获取对应的字段信息
    gateway = Gateway(
        loop=loop, store=storage.sessions,
        memory_context=memory.context if memory is not None else None,
        workers=config.workers, event_bus=events, action_runtime=action, perception_trace=perception_trace,
    )
    runtime = AgentRuntime(
        config=config, gateway=gateway, loop=loop, models=models, tools=tools,
        skills=skills, storage=storage, memory=memory, action=action,
        events=events, perception_trace=perception_trace, tool_handler=handler, tool_dispatch=dispatch,
        environment=EnvironmentSnapshotter(storage.executions) if config.use_memory else None,
        scheduler=CronScheduler(gateway, cron_jobs, mailbox=NeteaseMailbox.from_env()),
    )
    gateway.describe_agent = runtime.describe
    gateway.on_turn_committed = runtime.on_turn_committed
    gateway.on_session_end = runtime.on_session_end
    gateway.register_channel(FrontierChannel())
    return runtime
