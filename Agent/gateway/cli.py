"""
CLI 入口 — 双队列 Gateway + CliChannel

运行:
    python -m agent.gateway.cli
    python -m agent.gateway.cli --mock
    python -m agent.gateway.cli --mock --memory   # 不落盘

流程:
    终端 input
      → CliChannel.parse_inbound → InboundEvent
      → Gateway.submit / ask（入 in_dto）
      → worker 按 session_key 串行 → AgentLoop
      → OutboundReply（入 out_dto）→ CliChannel.deliver
"""
from __future__ import annotations

import argparse
import time


class _MockModel:
    def chat(self, messages: list[dict]) -> str:
        users = [m["content"] for m in messages if m.get("role") == "user"]
        last = users[-1] if users else ""
        return f"[mock] 第{len(users)}轮 | {last!r} | ctx={len(messages)}"


def main() -> None:
    parser = argparse.ArgumentParser(description="DemoCursor Gateway CLI")
    parser.add_argument("--mock", action="store_true", help="MockModel，不调真实 LLM")
    parser.add_argument(
        "--memory",
        action="store_true",
        help="使用内存 SessionStore（默认 JSONL 落盘到 data/sessions）",
    )
    parser.add_argument("--workers", type=int, default=2, help="入站并行 worker 数（按 session 仍串行）")
    args = parser.parse_args()

    from logging.loggingTool import setup_logging
    import logging

    setup_logging()
    log = logging.getLogger(__name__)

    from ..channel.cli import CliChannel
    from ..channel.wechat import WechatChannel
    from ..core.loop import AgentLoop
    from .runner import Gateway
    from .session_store import JsonlSessionStore, MemorySessionStore

    store = MemorySessionStore() if args.memory else JsonlSessionStore()
    if args.mock:
        model = _MockModel()
        gw = Gateway(model=model, loop=AgentLoop(model), store=store, workers=args.workers)
        mode = "mock"
    else:
        gw = Gateway(store=store, workers=args.workers)
        mode = "deepseek"

    # quiet=True：ask() 自己 print，避免 outbound worker 再打一遍
    cli = CliChannel(quiet=True)
    gw.register_channel(cli)
    gw.register_channel(WechatChannel())  # 注册但本入口不驱动；可供后续 submit_raw("wechat", ...)
    gw.start()

    print(f"Gateway CLI [{mode}] store={'memory' if args.memory else 'jsonl'}")
    print("命令: /new 新会话 | exit 退出")
    print("数据流: parse_inbound → in_dto → session锁+loop → out_dto → deliver")
    log.info("gateway cli ready")

    try:
        while True:
            try:
                raw = input("you > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            if raw.lower() in {"exit", "quit"}:
                break

            # 演示：既可传 str，也可传 {"content": ...}（Channel 内映射成 text）
            event = cli.parse_inbound(raw)
            t0 = time.time()
            reply = gw.ask(event, timeout=180.0)
            dt = time.time() - t0
            if reply.completed:
                print(f"ai  > {reply.text}")
            else:
                print(f"ai  > [error] {reply.error or reply.text}")
            print(f"     (session={reply.session_id[:8]}… key={reply.session_key} {dt:.2f}s)")
    finally:
        gw.stop()


if __name__ == "__main__":
    main()
