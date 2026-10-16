"""
后端装配与启动逻辑

build_runtime() 装配 AgentRuntime，模块职责见 agent/core/runtime.py：
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
    控制记忆快照、工具与后台提炼；会话始终保存到 SQLite。
只读观察：
    --enable-memory-debug / --no-enable-memory-debug（默认开启）
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .core.channel.channel_cli import CliChannel
from .core.channel.registry import autostart_channels
from agent.core.gateway.http_api import FrontierHttpServer
from agent.core.runtime import AgentRuntime, build_runtime
from agent.types.runtime import RuntimeConfig

logger = logging.getLogger(__name__)


def _print_runtime_banner(runtime: AgentRuntime) -> None:
    description = runtime.describe()
    print(f"[runtime] model={description.model} memory={description.memory} action={description.action} storage={description.storage}")
    print(f"[runtime] tools={', '.join(description.tools)}")
    if description.database is not None:
        print(f"[storage] SQLite 数据库: {description.database}")
    if description.memory:
        print(f"[memory] Markdown 分册目录: {Path(description.database).parent / 'memory'}")
        print("[memory] Reviewer: 按 1/2/3/4/5 轮渐进批处理；有效卡片直接入库")
        print("[memory] MEMORY.md 有界常驻；经验关键词最多召回 3 张；其余内容按工具检索")
    else:
        print("[memory] 已关闭")


def _run_http(port: int, *, use_memory: bool, enable_memory_debug: bool, use_action: bool = False) -> None:
    """HTTP 模式：Gateway + FrontierHttpServer，供 frontier 前端调用。"""
    from .core.provider import AGENT_HTTP_ALLOWED_ORIGINS, AGENT_HTTP_TOKEN

    runtime = build_runtime(RuntimeConfig(use_memory=use_memory, use_action=use_action))
    gateway = runtime.gateway
    runtime.start()
    autostart_channels(gateway)
    for ch in (gateway.get_channel("frontier"),):
        ch and ch.start()
    server = FrontierHttpServer(
        gateway, port=port, enable_memory_debug=enable_memory_debug,
        observe_databases={"agent": runtime.storage.db.path},
        api_token=AGENT_HTTP_TOKEN,
        allowed_origins=AGENT_HTTP_ALLOWED_ORIGINS,
    )
    print(f"[backend] HTTP API 已启动: http://127.0.0.1:{port}")
    if AGENT_HTTP_TOKEN is None:
        print("[backend] 未配置 AGENT_HTTP_TOKEN：仅限本机访问，浏览器 Origin 白名单仍生效")
    print("[backend] 前端开发: cd frontier && npm run dev")
    if enable_memory_debug:
        print("[backend] Memory Debug API 已开启（/api/debug/memory/*）")
    _print_runtime_banner(runtime)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.stop()
        print("[backend] 已停止")


def _run_cli(*, use_memory: bool, use_action: bool = False) -> None:
    """终端模式：注册 CliChannel，同步一问一答。"""
    # 管道输入（echo ... | python main.py --cli）按 UTF-8 解码；
    # 交互式终端保持系统编码不动（Windows 中文控制台是 GBK）
    import sys
    if not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    runtime = build_runtime(RuntimeConfig(use_memory=use_memory, use_action=use_action))
    gateway = runtime.gateway
    # CliChannel 的出站投递不打印：CLI 模式用 ask() 的同步返回值自己打印
    cli = CliChannel()
    gateway.register_channel(cli)
    runtime.start()
    autostart_channels(gateway)
    cli.start()
    print("[cli] 已进入对话模式，/new 重开会话，Ctrl+C 退出")
    _print_runtime_banner(runtime)
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
                print(f"[storage] persistence_error: {persistence}")
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
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
        help="启用记忆快照、工具与后台提炼（默认开；用 --no-use-memory 关闭；会话始终保存到 SQLite）",
    )
    parser.add_argument(
        "--enable-memory-debug",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="开启只读 Memory Debug HTTP（/api/debug/memory/*；默认开；用 --no-enable-memory-debug 关闭）",
    )
    parser.add_argument("--use-action", 
        action=argparse.BooleanOptionalAction, 
        default=True,
        help="启用 action 执行账本、工具集与 PRE/POST（默认开）")
    args = parser.parse_args()

    if args.cli:
        _run_cli(use_memory=args.use_memory, use_action=args.use_action)
    else:
        _run_http(
            args.port,
            use_memory=args.use_memory,
            enable_memory_debug=args.enable_memory_debug,
            use_action=args.use_action,
        )


if __name__ == "__main__":
    main()
