"""
后端装配与启动逻辑

build_gateway() 把各层组装成可运行的 Gateway：
    ModelRegistry（provider 访问入口）
    ToolRegistry + calculator + use_skill（+ 可选 memory 工具）
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
from .core.events import default_event_bus
from .core.loop import AgentLoop
from .gateway import FrontierHttpServer, Gateway
from .model.registry import ModelRegistry
from .skills import SkillManageTool, SkillRegistry, UseSkillTool
from .tools.calculator_tools import CalculatorTool
from .tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _memory_paths(root: Path | None = None) -> tuple[Path, Path]:
    """返回 (memory_root, memory.db)。root 默认取项目根下 data/。"""
    if root is None:
        from .Provider import ROOT

        root = ROOT / "data"
    memory_root = root / "memory"
    db_path = root / "memory.db"
    return memory_root, db_path


def build_gateway(*, workers: int = 2, use_memory: bool = True) -> Gateway:
    """组装后端：注册表 → Loop → Gateway → Channel。

    use_memory=True 时：
      - SqliteSessionStore 持久化 transcript
      - MemoryManager 注入每 turn 记忆快照
      - 注册 memory_read / memory_edit 工具
    use_memory=False 时：保持原 DictSessionStore 内存路径。
    """
    # ---- provider / 模型 ----
    model_registry = ModelRegistry.default()

    # ---- 技能 + 工具（use_skill / skill_manage 是技能体系入口）----
    from .Provider import ROOT as _ROOT

    skill_registry = SkillRegistry()
    skills_root = _ROOT / "data" / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    tool_registry = ToolRegistry()
    tool_registry.register(CalculatorTool())
    tool_registry.register(UseSkillTool(skill_registry))
    tool_registry.register(SkillManageTool(skill_registry, skills_root=skills_root))

    store = None
    memory = None
    reviewer_worker = None
    on_turn_committed = None
    on_session_end = None
    signal_collector = None
    assembly_policy = None
    assembly_recorder = None
    skill_selector = None
    if use_memory:
        from .evolution.compactor import GrowAndRefineCompactor
        from .evolution.memory_curator import PlaybookCurator
        from .evolution.reviewer import RuleBasedEvolutionReflector
        from .evolution.scheduler import EvolutionScheduler
        from .evolution.signals import EvolutionSignalCollector
        from .evolution.skill_creator import SkillCreator
        from .evolution.skill_gate import SkillLifecycleGate
        from .evolution.skill_optimizer import SkillOptimizer
        from .memory import MemoryManager, MemoryProvenance
        from .memory.episodic.store import (
            SqliteSessionArchiveReader,
            SqliteSessionStore,
        )
        from .memory.extractors import (
            FallbackMemoryExtractor,
            ModelBasedExtractor,
            RuleBasedExtractor,
        )
        from .memory.reviewer import TurnReviewer, ReviewerWorker
        from .tools.memory_tools import MemoryEditTool, MemoryReadTool

        memory_root, db_path = _memory_paths()
        memory_root.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        archive = SqliteSessionArchiveReader(db_path)
        store = SqliteSessionStore(db_path)
        memory = MemoryManager(
            root=memory_root,
            archive_reader=archive,
            compactor=GrowAndRefineCompactor(),
        )
        tool_registry.register(MemoryReadTool(memory))
        tool_registry.register(
            MemoryEditTool(
                memory,
                provenance_factory=lambda: MemoryProvenance(source="tool"),
            )
        )
        try:
            extract_model = model_registry.get()
        except Exception:
            extract_model = None
        if extract_model is not None:
            extractor = FallbackMemoryExtractor(
                ModelBasedExtractor(store, extract_model),
                RuleBasedExtractor(store),
            )
        else:
            extractor = RuleBasedExtractor(store)
        reviewer = TurnReviewer(store, memory, extractor)
        reviewer_worker = ReviewerWorker(reviewer)

        # 8.04：旁路进化 — playbook + Skill plugin 产物
        from .evolution.assembly import AssemblyPolicy, AssemblyRecorder
        from .evolution.audit import EvolutionAuditLog
        from .evolution.env_snapshot import EnvironmentSnapshotter
        from .evolution.held_out import HeldOutSelector, HeldOutTask
        from .evolution.llm_reflector import LlmEvolutionReflector
        from .evolution.skill_distiller import TrajectorySkillDistiller
        from .evolution.skill_selector import SkillSelector

        signal_collector = EvolutionSignalCollector(default_event_bus)
        reflector = RuleBasedEvolutionReflector(store)
        try:
            llm_reflector = LlmEvolutionReflector(model_registry.get())
        except Exception:
            llm_reflector = None
            logger.warning("LLM Reflector 未启用（无可用模型）")
        skill_distiller = TrajectorySkillDistiller(min_tools=5, frequency_threshold=3)
        skill_selector = SkillSelector()
        audit_log = EvolutionAuditLog(db_path.parent / "evolution_audit.db")
        held_out = HeldOutSelector(
            tasks=(
                HeldOutTask(
                    name="no_silent_skip",
                    forbidden_in_new=("静默跳过",),
                ),
            )
        )
        curator = PlaybookCurator(
            memory, held_out=held_out, audit_log=audit_log,
        )
        env_snapshotter = EnvironmentSnapshotter(db_path.parent / "env_snapshots.db")
        skill_creator = SkillCreator(
            skills_root, frequency_threshold=2, registry=skill_registry,
        )
        skill_optimizer = SkillOptimizer(skill_registry)
        skill_gate = SkillLifecycleGate(skill_registry, success_threshold=2)
        evolution = EvolutionScheduler()
        evolution.register(reviewer_worker.notify)

        def _user_text_for(turn) -> str:
            # 尽力从 store 取本 turn 用户消息；失败则空串
            try:
                if store is None:
                    return ""
                session = store.get_session(turn.session_id) if hasattr(store, "get_session") else None
                if session is None and hasattr(store, "get"):
                    session = store.get(turn.session_id)
                if session is None:
                    return ""
                for m in reversed(getattr(session, "messages", []) or []):
                    if getattr(m, "message_id", None) == turn.user_message_id:
                        return getattr(m, "content_text", "") or ""
                    if getattr(m, "role", None) == "user":
                        return getattr(m, "content_text", "") or ""
            except Exception:
                return ""
            return ""

        def _evolve_playbook(turn) -> None:
            signals = signal_collector.get(turn.turn_id)
            insights = list(reflector.reflect(turn, signals))
            if llm_reflector is not None:
                try:
                    insights.extend(
                        llm_reflector.reflect(
                            turn, signals, user_text=_user_text_for(turn),
                        )
                    )
                except Exception:
                    logger.exception("llm playbook reflect 失败 turn_id=%s", turn.turn_id)
            curator.apply_insights(insights)
            tips: list[str] = []
            if signals is not None:
                tips.extend(signals.tool_failures)
                if signals.error:
                    tips.append(signals.error)
            if turn.error:
                tips.append(turn.error)
            for tip in tips:
                curator.record_failure(turn.scope, tip, turn_id=turn.turn_id)

        def _evolve_skills(turn) -> None:
            signals = signal_collector.get(turn.turn_id)
            user_text = _user_text_for(turn)
            # Hermes：复杂成功轨迹 distill → proposal → 写盘
            proposal = skill_distiller.consider(turn, signals, user_text=user_text)
            if proposal is not None:
                skill_creator.create_from_proposal(proposal)
            if llm_reflector is not None:
                try:
                    for prop in llm_reflector.reflect_skills(
                        turn, signals, user_text=user_text,
                    ):
                        skill_creator.create_from_proposal(prop)
                except Exception:
                    logger.exception("llm skill reflect 失败 turn_id=%s", turn.turn_id)
            skill_creator.consider(turn, signals)
            skill_optimizer.optimize(turn, signals)
            if (
                signals is not None
                and signals.skills_used
                and not signals.tool_failures
                and turn.completed
                and not turn.error
            ):
                for name in signals.skills_used:
                    skill_gate.record_success(name)

        def _capture_env(turn) -> None:
            try:
                import os

                from .Provider import SYSTEM_PROMPT_PATH

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

        evolution.register(_evolve_playbook)
        evolution.register(_evolve_skills)
        evolution.register(_capture_env)
        evolution.register_periodic_refine(
            every_n=10,
            refine=curator.refine,
            scope_of=lambda turn: turn.scope,
        )
        on_turn_committed = evolution.notify
        on_session_end = reviewer_worker.notify
        assembly_policy = AssemblyPolicy()
        assembly_recorder = AssemblyRecorder()
        logger.info(
            "记忆已启用 root=%s db=%s reviewer=ModelBased→RuleBased "
            "evolution=hermes-loop+playbook+skills+assembly+heldout+audit+envsnap",
            memory_root, db_path,
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
        memory=memory,
        workers=workers,
        on_turn_committed=on_turn_committed,
        on_session_end=on_session_end,
        reviewer_worker=reviewer_worker,
        event_bus=default_event_bus,
        signal_collector=signal_collector,
        assembly_policy=assembly_policy,
        assembly_recorder=assembly_recorder,
        skill_selector=skill_selector,
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
    print(f"[memory] Hot/Daily 目录: {memory_root}")
    print(f"[memory] SQLite 数据库: {db_path}")
    print("[memory] Reviewer: 后台规则抽取（偏好→USER.md，约束→MEMORY.md，事件→daily/）")
    print("[memory] Evolution: Hermes 式 distill/select/manage + Phase0-5 旁路闭环")
    print("[memory] 可用工具: memory_read / memory_edit / skill_manage；/new 保留旧 transcript")


def _run_http(port: int, *, use_memory: bool) -> None:
    """HTTP 模式：Gateway + FrontierHttpServer，供 frontier 前端调用。"""
    gateway = build_gateway(use_memory=use_memory)
    gateway.start()
    for ch in (gateway.get_channel("frontier"),):
        ch and ch.start()
    server = FrontierHttpServer(gateway, port=port)
    print(f"[backend] HTTP API 已启动: http://127.0.0.1:{port}")
    print("[backend] 前端请另开终端运行: python frontier/main.py")
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
        help="启用记忆系统（SQLite + Hot/Daily；默认开；用 --no-use-memory 关闭）",
    )
    args = parser.parse_args()

    if args.cli:
        _run_cli(use_memory=args.use_memory)
    else:
        _run_http(args.port, use_memory=args.use_memory)


if __name__ == "__main__":
    main()
