"""
一键启动前端进程

用法：
    python frontier/main.py            # 启动静态服务（:5173）并自动打开浏览器
    python frontier/main.py --port 5174 --no-open

前端是纯静态页面（index.html + style.css + app.js），
通过 HTTP/SSE 连接后端（默认 http://127.0.0.1:8000，请先运行根目录 main.py）。
"""
from __future__ import annotations

import argparse
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 无论从哪里启动，都服务 frontier/ 目录本身
FRONTIER_DIR = Path(__file__).resolve().parent


class _QuietHandler(SimpleHTTPRequestHandler):
    """静音访问日志的静态文件处理器。"""

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="DemoCursor Agent 前端")
    parser.add_argument("--port", type=int, default=5173, help="监听端口")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    handler = partial(_QuietHandler, directory=str(FRONTIER_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"[frontier] 前端已启动: {url}")
    print("[frontier] 请确保后端已运行: python main.py")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[frontier] 已停止")


if __name__ == "__main__":
    main()
