"""
FrontierHttpServer — 可视化前端的 HTTP / SSE 入口（仅标准库实现）

供 frontier 前端接入的 Web 服务：
    POST /api/chat      : 发送消息，同步获取 Agent 回复（走完整 MessageBus 链路）
    GET  /api/events    : SSE 实时事件流（turn_start / llm_response / tool_call ...）
    GET  /api/status    : Gateway 运行状态（channel / 队列 / 会话 / 模型 / 工具）
    GET  /api/sessions  : 当前会话列表
    POST /api/reset     : 重开指定会话（等同 /new）
    GET  /api/debug/memory/session :（flag）当前用户 SQLite 会话只读视图
    GET  /api/debug/memory/sessions :（flag）当前用户全部 session（含已结束）
    GET  /api/debug/memory/turns :（flag）指定 session 的 turn 列表
    GET  /api/debug/memory/turn :（flag）指定 turn 的消息、工具和 Perception trace
    GET  /api/debug/memory/trace :（flag）指定 turn 的 Perception trace 指标
    GET  /api/debug/memory/context :（flag）当前用户冻结 MemorySnapshot 预览
    GET  /api/debug/tables         :（flag）已登记 SQLite 的表目录
    GET  /api/debug/tables/rows    :（flag）指定表的分页行
    GET  /api/debug/traces/modules :（flag）已注册 trace 模式
    GET  /api/debug/traces         :（flag）某一模式的 trace 列表

不引入 fastapi/flask：ThreadingHTTPServer 每个连接一个线程，
与 Gateway 的线程模型天然匹配；SSE 长连接就是一个阻塞读队列的线程。

访问控制（每个请求先过 _authorized）：
    - 带 Origin 且不在 allowed_origins 的请求直接 403：CORS 只拦浏览器读响应，
      拦不住跨站「简单请求」本身被执行
    - 配置 api_token 后，请求必须带 Authorization: Bearer <token>
    - 监听非本机地址时必须配置 api_token
这是操作者接口：body 中的 user_id / chat_id 由调用方指定（评测会用多身份）。
"""
from __future__ import annotations

import hmac
import json
import logging
import queue
from collections.abc import Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..channel.channel_frontier import FrontierChannel
from agent.types.events import LoopEvent
from ..gateway.session_key import build_session_key
from agent.core.memory.memory_scope import build_memory_scope
from ..storage.sqlite.catalog import CatalogError, list_catalog, read_table_page
from .gateway import Gateway
from .trace_modules import list_modules, list_traces

logger = logging.getLogger(__name__)

# SSE 心跳间隔（秒）：防止代理/浏览器静默断连
_SSE_HEARTBEAT = 15.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _event_to_json(event: LoopEvent) -> str:
    return event.model_dump_json()


class _Handler(BaseHTTPRequestHandler):
    """请求处理器。gateway 由 server 实例挂上来（self.server.gateway）。"""

    server: "FrontierHttpServer"

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_cors(self) -> None:
        origin = self.headers.get("Origin")
        if not origin or origin not in self.server.allowed_origins:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:
            self._send_json({"error": "origin not allowed"}, status=403)
            return False
        return True

    def _authorized(self) -> bool:
        if not self._origin_allowed():
            return False
        token = self.server.api_token
        if token is None:
            return True
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        if hmac.compare_digest(supplied, f"Bearer {token}".encode("utf-8")):
            return True
        self._send_json({"error": "unauthorized"}, status=401)
        return False

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        for encoding in ("utf-8", "gbk"):
            try:
                return json.loads(raw.decode(encoding))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return {}

    def _parse_path(self) -> tuple[str, dict]:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        # parse_qs → list；取首个标量
        query = {k: (v[0] if v else "") for k, v in qs.items()}
        return parsed.path, query

    def log_message(self, fmt, *args):
        logger.debug("http %s", fmt % args)

    def do_OPTIONS(self) -> None:
        # 预检请求不带 Authorization，只校验 Origin
        if not self._origin_allowed():
            return
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path, query = self._parse_path()
        if path == "/api/status":
            self._send_json(self.server.gateway.status())
        elif path == "/api/sessions":
            self._send_json({"sessions": self.server.gateway.store.list_sessions()})
        elif path == "/api/events":
            self._handle_sse()
        elif path == "/api/debug/memory/session":
            self._handle_debug_session(query)
        elif path == "/api/debug/memory/sessions":
            self._handle_debug_sessions(query)
        elif path == "/api/debug/memory/turns":
            self._handle_debug_turns(query)
        elif path == "/api/debug/memory/turn":
            self._handle_debug_turn(query)
        elif path == "/api/debug/memory/trace":
            self._handle_debug_trace(query)
        elif path == "/api/debug/memory/context":
            self._handle_debug_context(query)
        elif path == "/api/debug/tables":
            self._handle_debug_tables()
        elif path == "/api/debug/tables/rows":
            self._handle_debug_table_rows(query)
        elif path == "/api/debug/traces/modules":
            self._handle_debug_trace_modules()
        elif path == "/api/debug/traces":
            self._handle_debug_traces(query)
        else:
            self._send_json({"error": f"unknown path: {path}"}, status=404)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        path, _query = self._parse_path()
        if path == "/api/chat":
            self._handle_chat()
        elif path == "/api/reset":
            self._handle_reset()
        else:
            self._send_json({"error": f"unknown path: {path}"}, status=404)

    def _handle_chat(self) -> None:
        body = self._read_json_body()
        gateway = self.server.gateway
        channel = gateway.get_channel("frontier")
        if channel is None:
            self._send_json({"error": "frontier channel 未注册"}, status=500)
            return
        event = channel.parse_to_InboundEvent(body)
        if not event.text:
            self._send_json({"error": "text 不能为空"}, status=400)
            return
        reply = gateway.ask(event)
        self._send_json({
            "reply": reply.text,
            "error": (reply.metadata or {}).get("error"),
        })

    def _handle_reset(self) -> None:
        body = self._read_json_body()
        gateway = self.server.gateway
        channel = gateway.get_channel("frontier")
        if channel is None:
            self._send_json({"error": "frontier channel 未注册"}, status=500)
            return
        reply = gateway.ask(channel.parse_to_InboundEvent({**body, "text": "/new"}))
        self._send_json({"reply": reply.text})

    def _require_debug(self) -> bool:
        if not self.server.enable_memory_debug:
            self._send_json({"error": "memory debug disabled"}, status=404)
            return False
        return True

    def _handle_debug_session(self, query: dict) -> None:
        if not self._require_debug():
            return
        gateway = self.server.gateway
        source = FrontierChannel.build_source(query)
        session_key = build_session_key(source, agent_id=gateway.agent_id)
        requested_session_id = str(query.get("session_id") or "")
        session = (
            gateway.store.get_session(requested_session_id)
            if requested_session_id
            else gateway.store.find(session_key)
        )
        if session is None:
            self._send_json({
                "session_key": session_key,
                "session_id": None,
                "messages": [],
                "tool_calls": [],
                "message_count": 0,
                "status": None,
                "channel": source.channel,
                "author_id": source.user_id,
            })
            return
        self._send_json({
            "session_key": session.session_key,
            "session_id": session.session_id,
            "channel": session.channel,
            "author_id": session.author_id,
            "status": session.status,
            "message_count": session.message_count,
            "messages": [
                {
                    "message_id": m.message_id,
                    "role": m.role,
                    "content_text": m.content_text,
                    "created_at": m.created_at,
                    "seq_id": m.seq_id,
                }
                for m in session.messages
            ],
            "tool_calls": [
                tc.observation_payload() for tc in session.tool_calls
            ],
        })

    def _handle_debug_sessions(self, query: dict) -> None:
        if not self._require_debug():
            return
        source = FrontierChannel.build_source(query)
        scope = build_memory_scope(source)
        store = self.server.gateway.store
        reader = getattr(store, "list_debug_sessions", None)
        sessions = reader(scope_key=scope.storage_key) if reader else []
        self._send_json({"sessions": sessions, "scope_key": scope.storage_key})

    def _handle_debug_turns(self, query: dict) -> None:
        if not self._require_debug():
            return
        session_id = str(query.get("session_id") or "")
        store = self.server.gateway.store
        reader = getattr(getattr(store, "executions", None), "list_turns", None)
        turns = reader(session_id) if session_id and reader else []
        self._send_json({"session_id": session_id, "turns": turns})

    def _handle_debug_turn(self, query: dict) -> None:
        if not self._require_debug():
            return
        turn_id = str(query.get("turn_id") or "")
        store = self.server.gateway.store
        reader = getattr(getattr(store, "executions", None), "get_turn", None)
        turn = reader(turn_id) if turn_id and reader else None
        if turn is None:
            self._send_json({"error": "turn not found"}, status=404)
            return
        self._send_json(turn)

    def _handle_debug_trace(self, query: dict) -> None:
        if not self._require_debug():
            return
        turn_id = str(query.get("turn_id") or "")
        raw_iteration = query.get("iteration")
        iteration = None
        if raw_iteration is not None:
            try:
                iteration = int(str(raw_iteration))
                if iteration < 1:
                    raise ValueError()
            except ValueError:
                self._send_json({"error": "iteration must be a positive integer"}, status=400)
                return
        store = self.server.gateway.store
        reader = getattr(getattr(store, "executions", None), "get_turn", None)
        persisted_turn = reader(turn_id) if turn_id and reader else None
        persisted = persisted_turn.get("perception_trace") if persisted_turn else None
        trace = self.server.gateway.perception_trace
        latest = trace.get(turn_id) if trace and turn_id else (trace.latest() if trace else None)
        payload = persisted or (latest.model_dump(mode="json") if latest else None)
        builds = payload.get("builds", []) if isinstance(payload, dict) else []
        builds = [build for build in builds if isinstance(build, dict)]
        selected = (next((build for build in builds if build.get("iteration") == iteration), None)
                    if iteration is not None else builds[-1] if builds else None)
        reason = None
        if not builds:
            reason = "full_context_not_saved"
        elif selected is None:
            reason = "iteration_not_found"
        elif selected.get("messages") is None:
            reason = "context_build_failed"
        self._send_json({
            "turn_id": turn_id or (latest.trace_id if latest else None),
            "trace": {key: value for key, value in payload.items() if key != "builds"} if isinstance(payload, dict) else None,
            "window": trace.snapshot().model_dump(mode="json") if trace else None,
            "iterations": [{"iteration": build.get("iteration"), "available": build.get("messages") is not None,
                            "error": build.get("error")} for build in builds],
            "build": selected,
            "unavailable_reason": reason,
        })

    def _handle_debug_tables(self) -> None:
        if not self._require_debug():
            return
        self._send_json(list_catalog(self.server.observe_databases))

    def _handle_debug_table_rows(self, query: dict) -> None:
        if not self._require_debug():
            return
        try:
            limit = int(query.get("limit") or 50)
            offset = int(query.get("offset") or 0)
        except ValueError:
            self._send_json({"error": "limit and offset must be integers"}, status=400)
            return
        try:
            filters = json.loads(str(query.get("filters") or "{}"))
            if not isinstance(filters, dict):
                raise ValueError("filters must be an object")
            page = read_table_page(
                self.server.observe_databases,
                str(query.get("database") or ""),
                str(query.get("table") or ""),
                limit=limit,
                offset=offset,
                filters=filters,
            )
        except (CatalogError, ValueError, TypeError) as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json(page)

    def _handle_debug_trace_modules(self) -> None:
        if not self._require_debug():
            return
        self._send_json(list_modules())

    def _handle_debug_traces(self, query: dict) -> None:
        if not self._require_debug():
            return
        try:
            limit = int(query.get("limit") or 50)
            offset = int(query.get("offset") or 0)
        except ValueError:
            self._send_json({"error": "limit and offset must be integers"}, status=400)
            return
        try:
            filters = json.loads(str(query.get("filters") or "{}"))
            if not isinstance(filters, dict):
                raise ValueError("filters must be an object")
            payload = list_traces(
                self.server.observe_databases,
                str(query.get("module") or ""),
                session_id=str(query.get("session_id") or "") or None,
                limit=limit,
                offset=offset,
                filters=filters,
            )
        except (CatalogError, ValueError, TypeError) as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json(payload)

    def _handle_debug_context(self, query: dict) -> None:
        if not self._require_debug():
            return
        memory_context = getattr(self.server.gateway, "_memory_context", None)
        if memory_context is None:
            self._send_json({
                "snapshot": None,
                "error": "memory not enabled",
            })
            return
        source = FrontierChannel.build_source(query)
        scope = build_memory_scope(source)
        snapshot = memory_context.freeze(
            scope, session_id=str(query.get("session_id") or "debug-preview"), persist=False
        )
        self._send_json({
            "scope_kind": scope.scope_kind,
            "scope_key": scope.storage_key,
            "snapshot": snapshot.model_dump(mode="json"),
        })

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
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True
            bus.unsubscribe(q)
            logger.info("SSE 客户端断开")


class FrontierHttpServer(ThreadingHTTPServer):
    """挂着 Gateway 的 HTTP 服务。daemon_threads=True 保证主进程退出不卡。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        gateway: Gateway,
        host: str = "127.0.0.1",
        port: int = 8000,
        *,
        enable_memory_debug: bool = False,
        observe_databases: Mapping[str, Path] | None = None,
        api_token: str | None = None,
        allowed_origins: Iterable[str] = (),
    ):
        if host not in _LOOPBACK_HOSTS and not api_token:
            raise ValueError(f"监听非本机地址 {host} 时必须配置 api_token")
        self.gateway = gateway
        self.enable_memory_debug = enable_memory_debug
        self.observe_databases = dict(observe_databases or {})
        self.api_token = api_token or None
        self.allowed_origins = frozenset(allowed_origins)
        super().__init__((host, port), _Handler)
        logger.info(
            "HTTP API 就绪: http://%s:%d (memory_debug=%s token=%s origins=%s)",
            host,
            port,
            enable_memory_debug,
            self.api_token is not None,
            sorted(self.allowed_origins),
        )
