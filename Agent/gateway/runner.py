"""
Gateway — 进程内消息枢纽（双队列 + 按 session_key 串行）

思想：
    1. Channel 只负责 parse_inbound / deliver，把消息 put 进 in_dto
    2. inbound worker 取出 InboundEvent → build_session_key → 串行跑 Loop
    3. 结果写成 OutboundReply put 进 out_dto
    4. outbound worker 按 source.channel 找到 Channel.deliver

同 session_key 串行：每个 key 一把锁，避免多渠道/连击把同一会话打断。
不同 session_key 可并行（worker 池）。

这不是额外 OS 进程；要分布式时再把 Queue 换成 Redis List。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..core.types import (
    Message,
    ContentPart,
    InboundEvent,
    OutboundReply,
)
from ..core.loop import AgentLoop
from ..model.deepseek import DeepSeekModel
from ..channel.base import BaseChannel
from .queues import GatewayQueues
from .session_key import build_session_key
from .session_store import SessionStore, JsonlSessionStore

logger = logging.getLogger(__name__)


class Gateway:
    def __init__(
        self,
        *,
        model=None,
        loop: AgentLoop | None = None,
        store: SessionStore | None = None,
        agent_id: str = "main",
        workers: int = 2,
    ):
        self.agent_id = agent_id
        self.model = model or DeepSeekModel()
        self.loop = loop or AgentLoop(self.model)
        self.store = store or JsonlSessionStore()
        self.queues = GatewayQueues()
        self._channels: dict[str, BaseChannel] = {}
        self._session_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="gw-in")
        self._out_thread: threading.Thread | None = None
        self._running = False
        self._reply_waiters: dict[str, list] = {}  # correlation 可选；CLI 同步等待用
        self._waiter_lock = threading.Lock()

    # ---- channel 注册 ----
    def register_channel(self, channel: BaseChannel) -> None:
        self._channels[channel.name] = channel
        logger.info("registered channel=%s", channel.name)

    def get_channel(self, name: str) -> BaseChannel | None:
        return self._channels.get(name)

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._out_thread = threading.Thread(
            target=self._outbound_loop, name="gw-out", daemon=True
        )
        self._out_thread.start()
        # 持续从 in_dto 取事件丢进线程池（池内再按 session 加锁）
        t = threading.Thread(target=self._inbound_loop, name="gw-in-dispatch", daemon=True)
        t.start()
        logger.info("gateway started workers=%s channels=%s", self._executor._max_workers, list(self._channels))

    def stop(self) -> None:
        self._running = False
        self.queues.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("gateway stopping")

    # ---- Channel / 外部入口 ----
    def submit(self, event: InboundEvent) -> None:
        """异步：解析好的 InDTO 入队。"""
        self.queues.inbound.put(event)

    def submit_raw(self, channel_name: str, raw) -> None:
        """原始平台载荷：先走 Channel.parse_inbound，再入队。"""
        ch = self._channels.get(channel_name)
        if ch is None:
            raise KeyError(f"channel not registered: {channel_name}")
        event = ch.parse_inbound(raw)
        self.submit(event)

    def ask(self, event: InboundEvent, *, timeout: float = 120.0) -> OutboundReply:
        """
        同步等待一轮回复（CLI 友好）。
        仍走双队列；用 correlation_id 在 out 侧唤醒。
        """
        corr = f"corr-{time.time_ns()}"
        event.metadata = dict(event.metadata or {})
        event.metadata["correlation_id"] = corr
        event_box: list[OutboundReply | None] = [None]
        done = threading.Event()

        def _cb(reply: OutboundReply) -> None:
            event_box[0] = reply
            done.set()

        with self._waiter_lock:
            self._reply_waiters[corr] = [_cb]
        self.submit(event)
        if not done.wait(timeout=timeout):
            with self._waiter_lock:
                self._reply_waiters.pop(corr, None)
            return OutboundReply(
                text="",
                source=event.source,
                completed=False,
                error="timeout waiting for agent reply",
            )
        with self._waiter_lock:
            self._reply_waiters.pop(corr, None)
        return event_box[0] or OutboundReply(
            text="", source=event.source, completed=False, error="empty reply"
        )

    # ---- 内部：入站调度 ----
    def _inbound_loop(self) -> None:
        while self._running:
            try:
                event = self.queues.inbound.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                break
            self._executor.submit(self._process_inbound, event)

    def _lock_for(self, session_key: str) -> threading.Lock:
        with self._locks_guard:
            if session_key not in self._session_locks:
                self._session_locks[session_key] = threading.Lock()
            return self._session_locks[session_key]

    def _process_inbound(self, event: InboundEvent) -> None:
        key = build_session_key(event.source, agent_id=self.agent_id)
        lock = self._lock_for(key)
        with lock:
            try:
                reply = self._run_turn_locked(key, event)
            except Exception as e:
                logger.exception("process inbound failed key=%s", key)
                reply = OutboundReply(
                    text="",
                    source=event.source,
                    reply_to=event.source.reply_to_message_id,
                    session_key=key,
                    completed=False,
                    error=str(e),
                    metadata={"correlation_id": event.metadata.get("correlation_id")},
                )
        self.queues.outbound.put(reply)

    def _run_turn_locked(self, session_key: str, event: InboundEvent) -> OutboundReply:
        # 斜杠命令（gateway 短路，不进 loop）
        text = (event.text or "").strip()
        if text in {"/new", "/reset"}:
            session = self.store.reset(
                session_key,
                channel=event.source.channel,
                author_id=event.source.user_id or "",
            )
            return OutboundReply(
                text=f"已新开会话 session_id={session.session_id}",
                source=event.source,
                reply_to=event.source.reply_to_message_id,
                session_key=session_key,
                session_id=session.session_id,
                completed=True,
                metadata={"correlation_id": event.metadata.get("correlation_id")},
            )

        session = self.store.get_or_create(
            session_key,
            channel=event.source.channel,
            author_id=event.source.user_id or "",
        )
        user_msg = Message(
            role="user",
            content_text=event.text,
            content=event.content or [ContentPart(type="text", text=event.text)],
            author_id=event.source.user_id or "",
        )
        result = self.loop.run_turn(
            session,
            user_msg,
            ephemeral_prompt=event.channel_prompt,
        )
        # persist（JSONL）
        self.store.save(session)

        text_out = result.final_text if result.completed else (result.error or "error")
        return OutboundReply(
            text=text_out or "",
            source=event.source,
            reply_to=event.source.reply_to_message_id,
            session_key=session_key,
            session_id=result.session_id,
            completed=result.completed,
            error=result.error,
            metadata={"correlation_id": event.metadata.get("correlation_id")},
        )

    # ---- 内部：出站分发 ----
    def _outbound_loop(self) -> None:
        while self._running:
            try:
                reply = self.queues.outbound.get(timeout=0.5)
            except queue.Empty:
                continue
            if reply is None:
                break
            corr = (reply.metadata or {}).get("correlation_id")
            if corr:
                with self._waiter_lock:
                    waiters = self._reply_waiters.get(corr, [])
                for cb in waiters:
                    try:
                        cb(reply)
                    except Exception:
                        logger.exception("reply waiter failed")
            ch = self._channels.get(reply.source.channel)
            if ch is None:
                logger.warning("no channel for outbound channel=%s", reply.source.channel)
                continue
            try:
                ch.deliver(reply)
            except Exception:
                logger.exception("deliver failed channel=%s", reply.source.channel)


# 兼容旧名
GatewayRunner = Gateway
