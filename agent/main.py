"""
后端装配与启动逻辑

build_gateway() 把各层组装成可运行的 Gateway：
    ModelRegistry（provider 访问入口）
    ToolRegistry + calculator + use_skill
    SkillRegistry（扫描 builtin 技能）
    AgentLoop（多步对话核心）
    Gateway + MessageBus + SessionStore（消息枢纽；可选 Memory）

main() 提供两种运行模式：
    python main.py            → HTTP 模式：frontier 前端的后端（默认 :8000）
    python main.py --cli      → 终端对话模式

记忆开关：
    --use-memory / --no-use-memory（默认开启）
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .channel.channel_cli import CliChannel
from .channel.channel_frontier import FrontierChannel
from .channel.registry import autostart_channels
from .core.event_bus import default_event_bus
from .core.agent_loop import AgentLoop
from .gateway import FrontierHttpServer, Gateway
from .model.registry import ModelRegistry
from .skills import SkillManageTool, SkillRegistry, UseSkillTool
from .tools.calculator_tools import CalculatorTool
from .tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _memory_paths(root: Path | None = None) -> tuple[Path, Path]:
    """返回 (memory_root, agent.db)。root 默认取项目根下 data/。"""
    if root is None:
        from .provider import ROOT

        root = ROOT / "data"
    memory_root = root / "memory"
    db_path = root / "agent.db"
    return memory_root, db_path


def build_gateway(*, workers: int = 2, use_memory: bool = True) -> Gateway:
    """组装后端：注册表 → Loop → Gateway → Channel。

    use_memory=True 时：
      - SqliteSessionStore 持久化 transcript
      - MemoryContextLoader 冻结 Policy / Project / User Markdown 快照
      - SessionMemoryReviewer 异步提案并原子写入结构化记忆
    use_memory=False 时：保持原 DictSessionStore 内存路径。
    """
    # ---- provider / 模型 ----
    model_registry = ModelRegistry.default()

    # ---- 技能 + 工具（use_skill / skill_manage 是技能体系入口）----
    from .provider import ROOT as _ROOT
    from .skills.repository import SkillRepository

    skill_registry = SkillRegistry()
    skills_root = _ROOT / "data" / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    skill_repository = SkillRepository(skills_root, registry=skill_registry)
    tool_registry = ToolRegistry()
    tool_registry.register(CalculatorTool())
    tool_registry.register(UseSkillTool(skill_registry))
    tool_registry.register(SkillManageTool(skill_repository))

    store = None
    memory_context = None
    reviewer_worker = None
    on_turn_committed = None
    on_session_end = None
    signal_collector = None
    assembly_policy = None
    assembly_recorder = None
    skill_selector = None
    observe_databases = None
    if use_memory:
        from .evolution.scheduler import EvolutionScheduler
        from .evolution.signals import EvolutionSignalCollector
        from .evolution.skill_creator import SkillCreator
        from .evolution.skill_evolution import SkillEvolution
        from .evolution.skill_gate import SkillLifecycleGate
        from .evolution.skill_optimizer import SkillOptimizer
        from .core.memory import (
            FallbackMemoryExtractor,
            MemoryContextLoader,
            MemoryValidator,
            ModelMemoryExtractor,
            RuleBasedMemoryExtractor,
            SessionMemoryReviewer,
            SessionMemoryReviewWorker,
        )
        from .storage.sqlite import RuntimeStorage

        memory_root, db_path = _memory_paths()
        memory_root.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        rule_extractor = RuleBasedMemoryExtractor()
        try:
            extractor = FallbackMemoryExtractor(
                ModelMemoryExtractor(model_registry.get()),
                rule_extractor,
            )
        except Exception:
            extractor = rule_extractor
        storage = RuntimeStorage.open(
            db_path,
            extractor_version=extractor.version,
        )
        store = storage.sessions
        project_books = tuple(
            path
            for path in (_ROOT / "project.md", _ROOT / "AGENTS.md", _ROOT / "CLAUDE.md")
            if path.exists()
        )
        memory_context = MemoryContextLoader(
            root=memory_root,
            policy_paths=(memory_root / "POLICY.md",),
            project_paths=project_books,
            document_registry=storage.documents,
        )
        reviewer = SessionMemoryReviewer(
            extractor,
            validator=MemoryValidator(),
        )
        reviewer_worker = SessionMemoryReviewWorker(
            store,
            reviewer,
            storage.review_committer,
            extractor_version=extractor.version,
        )

        # 8.04：旁路进化保留 Skill 产物；旧 marker playbook 写入链已移除。
        from .evolution.assembly import AssemblyPolicy, AssemblyRecorder
        from .evolution.env_snapshot import EnvironmentSnapshotter
        from .evolution.llm_reflector import LlmEvolutionReflector
        from .evolution.skill_distiller import TrajectorySkillDistiller
        from .evolution.skill_selector import SkillSelector

        signal_collector = EvolutionSignalCollector(default_event_bus)
        try:
            llm_reflector = LlmEvolutionReflector(model_registry.get())
        except Exception:
            llm_reflector = None
            logger.warning("LLM Reflector 未启用（无可用模型）")
        skill_distiller = TrajectorySkillDistiller(min_tools=5, frequency_threshold=3)
        skill_selector = SkillSelector()
        audit_log = storage.audit
        skill_repository._audit = audit_log
        env_snapshotter = EnvironmentSnapshotter(storage.executions)
        skill_creator = SkillCreator(
            skills_root, frequency_threshold=2, registry=skill_registry,
            repository=skill_repository,
        )
        skill_optimizer = SkillOptimizer(skill_registry)
        skill_gate = SkillLifecycleGate(
            skill_registry, repository=skill_repository,
        )
        skill_evolution = SkillEvolution(
            jobs=storage.jobs,
            store=store,
            creator=skill_creator,
            distiller=skill_distiller,
            optimizer=skill_optimizer,
            gate=skill_gate,
            repository=skill_repository,
            llm_reflector=llm_reflector,
        )
        evolution = EvolutionScheduler()
        evolution.register(reviewer_worker.notify)

        def _evolve_skills(turn) -> None:
            skill_evolution.consider(turn)

        def _capture_env(turn) -> None:
            try:
                import os

                from .provider import SYSTEM_PROMPT_PATH

                skill_versions = {
                    name: (
                        skill_registry.get(name).status
                        if skill_registry.get(name) else "unknown"
                    )
                    for name in skill_registry.list_skills()
                }
                tool_schemas = tool_registry.get_schemas()
                env_snapshotter.capture(
                    session_id=turn.session_id,
                    turn_id=turn.turn_id,
                    cwd=os.getcwd(),
                    model_name=None,
                    system_prompt_path=SYSTEM_PROMPT_PATH,
                    tool_schemas=tool_schemas,
                    skill_versions=skill_versions,
                )
            except Exception:
                logger.exception("env snapshot 失败 turn_id=%s", turn.turn_id)

        evolution.register(_evolve_skills)
        evolution.register(_capture_env)
        on_turn_committed = evolution.notify
        memory_reviewer = reviewer_worker
        on_session_end = memory_reviewer.notify

        class _LifecycleBundle:
            def start(_self) -> None:
                memory_reviewer.start()
                skill_evolution.worker.start()

            def stop(_self) -> None:
                skill_evolution.worker.stop()
                memory_reviewer.stop()

        reviewer_worker = _LifecycleBundle()
        assembly_policy = AssemblyPolicy()
        assembly_recorder = AssemblyRecorder()
        observe_databases = {"agent": db_path}
        logger.info(
            "记忆已启用 root=%s db=%s reviewer=%s "
            "evolution=hermes-skills+assembly+audit+envsnap",
            memory_root, db_path, extractor.version,
        )

    # ---- 对话核心 ----
    loop = AgentLoop(
        model_registry=model_registry,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        event_bus=default_event_bus,
    )

    # ---- 消息枢纽 ----
    gateway = Gateway(
        loop=loop,
        store=store,
        memory_context=memory_context,
        workers=workers,
        on_turn_committed=on_turn_committed,
        on_session_end=on_session_end,
        reviewer_worker=reviewer_worker,
        event_bus=default_event_bus,
        signal_collector=signal_collector,
        assembly_policy=assembly_policy,
        assembly_recorder=assembly_recorder,
        skill_selector=skill_selector,
        observe_databases=observe_databases,
    )
    gateway.register_channel(FrontierChannel())
    # 平台 channel 自动启用：registry.json 未显式关闭且凭据（env/JSON）填齐即连接
    autostart_channels(gateway)
    return gateway


def _print_memory_banner(use_memory: bool) -> None:
    if not use_memory:
        print("[memory] 已关闭（--no-use-memory）；会话仅存内存，重启即丢")
        return
    memory_root, db_path = _memory_paths()
    print("[memory] 已启用")
    print(f"[memory] Markdown 分册目录: {memory_root}")
    print(f"[memory] SQLite 数据库: {db_path}")
    print("[memory] Reviewer: Model Proposal → Validator → SQLite 原子提交")
    print("[memory] Evolution: Hermes 式 Skill distill/select/manage")
    print("[memory] MEMORY.md 全量加载；召回工具将在检索阶段接入")


def _run_http(port: int, *, use_memory: bool, enable_memory_debug: bool) -> None:
    """HTTP 模式：Gateway + FrontierHttpServer，供 frontier 前端调用。"""
    gateway = build_gateway(use_memory=use_memory)
    gateway.start()
    for ch in (gateway.get_channel("frontier"),):
        ch and ch.start()
    server = FrontierHttpServer(
        gateway, port=port, enable_memory_debug=enable_memory_debug
    )
    print(f"[backend] HTTP API 已启动: http://127.0.0.1:{port}")
    print("[backend] 前端开发: cd frontier && npm run dev")
    if enable_memory_debug:
        print("[backend] Memory Debug API 已开启（/api/debug/memory/*）")
    _print_memory_banner(use_memory)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        gateway.stop()
        print("[backend] 已停止")


def _run_cli(*, use_memory: bool) -> None:
    """终端模式：注册 CliChannel，同步一问一答。"""
    # 管道输入（echo ... | python main.py --cli）按 UTF-8 解码；
    # 交互式终端保持系统编码不动（Windows 中文控制台是 GBK）
    import sys
    if not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    gateway = build_gateway(use_memory=use_memory)
    # quiet=True：出站投递不打印 —— CLI 模式用 ask() 的同步返回值自己打印，
    # 否则 Gateway 出站分发 + 手动打印会重复输出
    cli = CliChannel(quiet=True)
    gateway.register_channel(cli)
    gateway.start()
    cli.start()
    print("[cli] 已进入对话模式，/new 重开会话，Ctrl+C 退出")
    _print_memory_banner(use_memory)
    try:
        while True:
            try:
                text = input("you > ").strip()
            except EOFError:
                break
            if not text:
                continue
            reply = gateway.ask(cli.parse_to_InboundEvent(text))
            error = (reply.metadata or {}).get("error")
            persistence = (reply.metadata or {}).get("persistence_error")
            if error:
                print(f"ai  > [error] {error}")
            else:
                print(f"ai  > {reply.text}")
            if persistence:
                print(f"[memory] persistence_error: {persistence}")
    except KeyboardInterrupt:
        pass
    finally:
        gateway.stop()
        print("\n[cli] 已退出")


def main() -> None:
    # 日志必须在最早配置（config.yml: LOG_PATH / LOG_LEVEL）
    from logging.loggingTool import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(description="DemoCursor Agent 后端")
    parser.add_argument("--cli", action="store_true", help="终端对话模式（默认 HTTP 模式）")
    parser.add_argument("--port", type=int, default=8000, help="HTTP 模式监听端口")
    parser.add_argument(
        "--use-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="启用记忆系统（分层 Markdown + SQLite；默认开；用 --no-use-memory 关闭）",
    )
    parser.add_argument(
        "--enable-memory-debug",
        action="store_true",
        default=False,
        help="开启只读 Memory Debug HTTP（/api/debug/memory/*；默认关）",
    )
    args = parser.parse_args()

    if args.cli:
        _run_cli(use_memory=args.use_memory)
    else:
        _run_http(
            args.port,
            use_memory=args.use_memory,
            enable_memory_debug=args.enable_memory_debug,
        )


if __name__ == "__main__":
    main()
