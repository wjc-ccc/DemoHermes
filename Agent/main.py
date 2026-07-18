"""
后端装配与启动逻辑

build_gateway() 把各层组装成可运行的 Gateway：
    ModelRegistry（provider 访问入口）
    ToolRegistry + calculator + use_skill（工具/技能调用链路）
    SkillRegistry（扫描 builtin 技能）
    AgentLoop（多步对话核心）
    Gateway + MessageBus + DictSessionStore（消息枢纽）

main() 提供两种运行模式：
    python main.py            → HTTP 模式：frontier 前端的后端（默认 :8000）
    python main.py --cli      → 终端对话模式
"""
from __future__ import annotations

import argparse
import logging

from .channel.channel_cli import CliChannel
from .channel.channel_frontier import FrontierChannel
from .core.loop import AgentLoop
from .gateway import FrontierHttpServer, Gateway
from .model.registry import ModelRegistry
from .skills import SkillRegistry, UseSkillTool
from .tools.calculator_tools import CalculatorTool
from .tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def build_gateway(*, workers: int = 2) -> Gateway:
    """组装后端：注册表 → Loop → Gateway → Channel。"""
    # ---- provider / 模型 ----
    model_registry = ModelRegistry.default()

    # ---- 技能 + 工具（use_skill 是技能体系的入口工具）----
    skill_registry = SkillRegistry()
    tool_registry = ToolRegistry()
    tool_registry.register(CalculatorTool())
    tool_registry.register(UseSkillTool(skill_registry))

    # ---- 对话核心 ----
    loop = AgentLoop(
        model_registry=model_registry,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
    )

    # ---- 消息枢纽 ----
    gateway = Gateway(loop=loop, workers=workers)
    gateway.register_channel(FrontierChannel())
    return gateway


def _run_http(port: int) -> None:
    """HTTP 模式：Gateway + FrontierHttpServer，供 frontier 前端调用。"""
    gateway = build_gateway()
    gateway.start()
    for ch in (gateway.get_channel("frontier"),):
        ch and ch.start()
    server = FrontierHttpServer(gateway, port=port)
    print(f"[backend] HTTP API 已启动: http://127.0.0.1:{port}")
    print("[backend] 前端请另开终端运行: python frontier/main.py")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        gateway.stop()
        print("[backend] 已停止")


def _run_cli() -> None:
    """终端模式：注册 CliChannel，同步一问一答。"""
    # 管道输入（echo ... | python main.py --cli）按 UTF-8 解码；
    # 交互式终端保持系统编码不动（Windows 中文控制台是 GBK）
    import sys
    if not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    gateway = build_gateway()
    # quiet=True：出站投递不打印 —— CLI 模式用 ask() 的同步返回值自己打印，
    # 否则 Gateway 出站分发 + 手动打印会重复输出
    cli = CliChannel(quiet=True)
    gateway.register_channel(cli)
    gateway.start()
    cli.start()
    print("[cli] 已进入对话模式，/new 重开会话，Ctrl+C 退出")
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
            print(f"ai  > [error] {error}" if error else f"ai  > {reply.text}")
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
    args = parser.parse_args()

    if args.cli:
        _run_cli()
    else:
        _run_http(args.port)


if __name__ == "__main__":
    main()
