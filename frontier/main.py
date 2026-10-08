"""
一键启动前端进程

用法：
    python frontier/main.py            # 托管 dist/（需先 npm run build）
    python frontier/main.py --port 5174 --no-open

开发请用：
    cd frontier && npm run dev
    （Vite 从 src/config.js 读取后端端口做 /api proxy）
"""
from __future__ import annotations

import argparse
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FRONTIER_DIR = Path(__file__).resolve().parent
DIST_DIR = FRONTIER_DIR / "dist"


class _QuietHandler(SimpleHTTPRequestHandler):
    """静音访问日志的静态文件处理器。"""

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="DemoCursor Agent 前端")
    parser.add_argument("--port", type=int, default=5173, help="监听端口")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    root = DIST_DIR if DIST_DIR.is_dir() else FRONTIER_DIR
    if root is FRONTIER_DIR:
        print("[frontier] 未找到 dist/，请先: cd frontier && npm run build")
        print("[frontier] 开发请用: cd frontier && npm run dev")

    handler = partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"[frontier] 前端已启动: {url} (root={root.name})")
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
