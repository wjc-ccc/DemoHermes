"""
Agent/gateway 包 — 进程内消息枢纽层

对外导出：
    Gateway            消息枢纽（Channel 注册 / 总线调度 / 会话串行 / 斜杠命令）
    MessageBus         入站/出站双队列
    DictSessionStore   session_key → Session 的内存字典存储
    build_session_key  会话键唯一构建公式
    FrontierHttpServer 可视化前端的 HTTP/SSE 入口
"""
from .gateway import Gateway
from .http_api import FrontierHttpServer
from .message_bus import MessageBus
from .session_key import build_session_key
from .session_store import DictSessionStore

__all__ = [
    "Gateway",
    "MessageBus",
    "DictSessionStore",
    "build_session_key",
    "FrontierHttpServer",
]
