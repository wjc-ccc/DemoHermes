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
from .core.events import default_event_bus
from .core.loop import AgentLoop
from .gateway import FrontierHttpServer, Gateway
from .model.registry import ModelRegistry
from .skills import SkillRegistry, UseSkillTool
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

    # ---- 技能 + 工具（use_skill 是技能体系的入口工具）----
    skill_registry = SkillRegistry()
    tool_registry = ToolRegistry()
    tool_registry.register(CalculatorTool())
    tool_registry.register(UseSkillTool(skill_registry))

    store = None
    memory = None
    reviewer_worker = None
    on_turn_committed = None
    on_session_end = None
    signal_collector = None
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
        from .memory.extractors import RuleBasedExtractor
        from .memory.reviewer import MemoryReviewer, ReviewerWorker
        from .Provider import ROOT
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
        reviewer = MemoryReviewer(
            store, memory, RuleBasedExtractor(store),
        )
        reviewer_worker = ReviewerWorker(reviewer)

        # 8.04：旁路进化 — playbook + Skill plugin 产物
        signal_collector = EvolutionSignalCollector(default_event_bus)
        reflector = RuleBasedEvolutionReflector(store)
        curator = PlaybookCurator(memory)
        skills_root = ROOT / "data" / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        skill_creator = SkillCreator(
            skills_root, frequency_threshold=2, registry=skill_registry,
        )
        skill_optimizer = SkillOptimizer(skill_registry)
        skill_gate = SkillLifecycleGate(skill_registry, success_threshold=2)
        evolution = EvolutionScheduler()
        evolution.register(reviewer_worker.notify)

        def _evolve_playbook(turn) -> None:
            signals = signal_collector.get(turn.turn_id)
            curator.apply_insights(reflector.reflect(turn, signals))
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

        evolution.register(_evolve_playbook)
        evolution.register(_evolve_skills)
        evolution.register_periodic_refine(
            every_n=10,
            refine=curator.refine,
            scope_of=lambda turn: turn.scope,
        )
        on_turn_committed = evolution.notify
        on_session_end = reviewer_worker.notify
        from .evolution.assembly import AssemblyPolicy, AssemblyRecorder

        assembly_policy = AssemblyPolicy()
        assembly_recorder = AssemblyRecorder()
        logger.info(
            "记忆已启用 root=%s db=%s reviewer=RuleBasedExtractor "
            "evolution=playbook+compactor+skills+assembly",
            memory_root, db_path,
        )
    else:
        assembly_policy = None
        assembly_recorder = None

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
    )
    gateway.register_channel(FrontierChannel())
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
    print("[memory] Evolution: playbook + Skill draft/active + AssemblyPolicy + Budget Skill 裁剪")
    print("[memory] 可用工具: memory_read / memory_edit；/new 保留旧 transcript")


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
