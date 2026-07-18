"""
FrontierHttpServer — 可视化前端的 HTTP / SSE 入口（仅标准库实现）

供 frontier 前端接入的 Web 服务：
    POST /api/chat      : 发送消息，同步获取 Agent 回复（走完整 MessageBus 链路）
    GET  /api/events    : SSE 实时事件流（turn_start / llm_response / tool_call ...）
    GET  /api/status    : Gateway 运行状态（channel / 队列 / 会话 / 模型 / 工具）
    GET  /api/sessions  : 当前内存中的会话列表
    POST /api/reset     : 重开指定会话（等同 /new）

不引入 fastapi/flask：ThreadingHTTPServer 每个连接一个线程，
与 Gateway 的线程模型天然匹配；SSE 长连接就是一个阻塞读队列的线程。
"""
from __future__ import annotations

import json
import logging
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..core.events import LoopEvent
from .gateway import Gateway

logger = logging.getLogger(__name__)

# SSE 心跳间隔（秒）：防止代理/浏览器静默断连
_SSE_HEARTBEAT = 15.0


def _event_to_json(event: LoopEvent) -> str:
    return event.model_dump_json()


class _Handler(BaseHTTPRequestHandler):
    """请求处理器。gateway 由 server 实例挂上来（self.server.gateway）。"""

    server: "FrontierHttpServer"  # 类型提示：自定义 server 上挂着 gateway

    # ---- 基础工具 ----
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_cors(self) -> None:
        # 前端跑在另一个端口（5173），必须放行跨域
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        # 浏览器固定发 UTF-8；Windows 终端 curl 可能发 GBK —— 解码做兜底
        for encoding in ("utf-8", "gbk"):
            try:
                return json.loads(raw.decode(encoding))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return {}

    def log_message(self, fmt, *args):  # 静音默认访问日志，走自己的 logger
        logger.debug("http %s", fmt % args)

    # ---- CORS 预检 ----
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    # ---- GET ----
    def do_GET(self) -> None:
        if self.path == "/api/status":
            self._send_json(self.server.gateway.status())
        elif self.path == "/api/sessions":
            self._send_json({"sessions": self.server.gateway.store.list_sessions()})
        elif self.path == "/api/events":
            self._handle_sse()
        else:
            self._send_json({"error": f"unknown path: {self.path}"}, status=404)

    # ---- POST ----
    def do_POST(self) -> None:
        if self.path == "/api/chat":
            self._handle_chat()
        elif self.path == "/api/reset":
            self._handle_reset()
        else:
            self._send_json({"error": f"unknown path: {self.path}"}, status=404)

    # ---- 聊天：HTTP 同步问一句 ----
    def _handle_chat(self) -> None:
        body = self._read_json_body()
        text = str(body.get("text", "")).strip()
        if not text:
            self._send_json({"error": "text 不能为空"}, status=400)
            return
        gateway = self.server.gateway
        channel = gateway.get_channel("frontier")
        if channel is None:
            self._send_json({"error": "frontier channel 未注册"}, status=500)
            return
        # 原始 dict 经 Channel 归一化为 InboundEvent，再走完整总线链路
        event = channel.parse_to_InboundEvent({
            "text": text,
            "chat_id": body.get("chat_id") or "web",
            "user_id": body.get("user_id") or "web-user",
        })
        reply = gateway.ask(event)
        self._send_json({
            "reply": reply.text,
            "error": (reply.metadata or {}).get("error"),
        })

    # ---- 重开会话 ----
    def _handle_reset(self) -> None:
        body = self._read_json_body()
        gateway = self.server.gateway
        channel = gateway.get_channel("frontier")
        event = channel.parse_to_InboundEvent({
            "text": "/new",
            "chat_id": body.get("chat_id") or "web",
            "user_id": body.get("user_id") or "web-user",
        })
        reply = gateway.ask(event)
        self._send_json({"reply": reply.text})

    # ---- SSE 事件流 ----
    def _handle_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors()
        self.end_headers()

        bus = self.server.gateway.events
        q = bus.subscribe()
        logger.info("SSE 客户端接入，当前订阅数=%d", len(bus._subscribers))
        try:
            while True:
                try:
                    event = q.get(timeout=_SSE_HEARTBEAT)
                    self.wfile.write(f"data: {_event_to_json(event)}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # 心跳
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端断开，正常收尾
        finally:
            bus.unsubscribe(q)
            logger.info("SSE 客户端断开")


class FrontierHttpServer(ThreadingHTTPServer):
    """挂着 Gateway 的 HTTP 服务。daemon_threads=True 保证主进程退出不卡。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, gateway: Gateway, host: str = "127.0.0.1", port: int = 8000):
        self.gateway = gateway
        super().__init__((host, port), _Handler)
        logger.info("HTTP API 就绪: http://%s:%d", host, port)
