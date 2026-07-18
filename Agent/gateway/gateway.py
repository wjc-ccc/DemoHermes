"""
Gateway — 进程内消息枢纽（MessageBus 双队列 + 按 session_key 串行）

核心能力：
    1. Channel 注册与管理（register_channel / get_channel）
    2. 消息构建（build_message：InboundEvent → Message，委托 core/messages 工厂）
    3. MessageBus 输入输出（submit/ask 入站，_outbound_dispatch_loop 出站分发）
    4. session_key 构建与上下文寻找（build_session_key + DictSessionStore）
    5. 同 session_key 串行执行（每 key 一把锁），不同 key 并行（线程池）

数据流：
    Channel.parse_to_InboundEvent(raw)
        → submit/ask → [in_dto]
        → worker：build_session_key → 加锁 → store.get_or_create → AgentLoop.run_turn
        → [out_dto] → 按 source.channel 路由 → Channel.deliver_to_OutboundEvent

斜杠命令（/new /reset /help）在 Gateway 短路处理，不进 Loop。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..channel.baseChannel import BaseChannel
from ..core.events import EventBus, LoopEvent, default_event_bus
from ..core.loop import AgentLoop
from ..core.messages import build_user_message
from ..core.types import InboundEvent, Message, OutboundReply
from .message_bus import MessageBus
from .session_key import build_session_key
from .session_store import DictSessionStore

logger = logging.getLogger(__name__)

# 同步 ask 的默认等待上限（秒）
ASK_TIMEOUT = 120.0


class Gateway:
    def __init__(
        self,
        *,
        loop: AgentLoop | None = None,
        store: DictSessionStore | None = None,
        bus: MessageBus | None = None,
        event_bus: EventBus | None = None,
        agent_id: str = "main",
        workers: int = 2,
    ):
        self.agent_id = agent_id
        self.loop = loop or AgentLoop()          # 对话核心（不认识平台）
        self.store = store or DictSessionStore()  # session_key → Session（dict）
        self.bus = bus or MessageBus()            # 入站/出站双队列
        self.events = event_bus or default_event_bus
        self._channels: dict[str, BaseChannel] = {}
        # 同 session_key 串行：每 key 一把锁
        self._session_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="gw-in")
        self._threads: list[threading.Thread] = []
        self._running = False
        # 同步 ask 的等待者：correlation_id → 回调
        self._reply_waiters: dict[str, list] = {}
        self._waiter_lock = threading.Lock()

    # ================= Channel 注册 =================
    def register_channel(self, channel: BaseChannel) -> None:
        """注册 Channel 并回绑 Gateway（Channel.send_gateway 才有去处）。"""
        self._channels[channel.name] = channel
        channel.bind_gateway(self)
        logger.info("注册 channel=%s", channel.name)

    def get_channel(self, name: str) -> BaseChannel | None:
        return self._channels.get(name)

    # ================= 生命周期 =================
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._threads = [
            threading.Thread(target=self._inbound_dispatch_loop, name="gw-in-dispatch", daemon=True),
            threading.Thread(target=self._outbound_dispatch_loop, name="gw-out-dispatch", daemon=True),
        ]
        for t in self._threads:
            t.start()
        logger.info(
            "gateway 已启动 workers=%s channels=%s",
            self._executor._max_workers, list(self._channels),
        )

    def stop(self) -> None:
        self._running = False
        self.bus.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("gateway 已停止")

    # ================= 消息构建 =================
    @staticmethod
    def build_message(event: InboundEvent) -> Message:
        """InboundEvent → role=user 的 Message（统一走 core/messages 工厂）。"""
        return build_user_message(event)

    # ================= 入站入口 =================
    def submit(self, event: InboundEvent) -> None:
        """异步：解析好的 InboundEvent 入队，不等待回复。"""
        self.bus.publish_inbound(event)

    def submit_raw(self, channel_name: str, raw) -> None:
        """原始平台载荷：先走 Channel.parse_to_InboundEvent，再入队。"""
        ch = self._channels.get(channel_name)
        if ch is None:
            raise KeyError(f"channel 未注册: {channel_name}")
        self.submit(ch.parse_to_InboundEvent(raw))

    def ask(self, event: InboundEvent, *, timeout: float = ASK_TIMEOUT) -> OutboundReply:
        """
        同步问一句：仍走完整 MessageBus 链路，
        用 correlation_id 在出站侧唤醒等待者（HTTP / CLI 同步场景用）。
        """
        corr = f"corr-{time.time_ns()}"
        event.metadata = dict(event.metadata or {})
        event.metadata["correlation_id"] = corr
        done = threading.Event()
        box: list[OutboundReply | None] = [None]

        def _cb(reply: OutboundReply) -> None:
            box[0] = reply
            done.set()

        with self._waiter_lock:
            self._reply_waiters[corr] = [_cb]
        self.submit(event)
        try:
            if not done.wait(timeout=timeout):
                return OutboundReply(
                    text="",
                    source=event.source,
                    metadata={"error": "等待 Agent 回复超时"},
                )
            return box[0] or OutboundReply(
                text="", source=event.source, metadata={"error": "空回复"}
            )
        finally:
            with self._waiter_lock:
                self._reply_waiters.pop(corr, None)

    # ================= 内部：入站调度 =================
    def _inbound_dispatch_loop(self) -> None:
        """持续从 in_dto 取事件丢进线程池（池内再按 session_key 加锁串行）。"""
        while self._running:
            try:
                event = self.bus.next_inbound(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:  # 结束信号
                break
            self._executor.submit(self._process_inbound, event)

    def _lock_for(self, session_key: str) -> threading.Lock:
        with self._locks_guard:
            if session_key not in self._session_locks:
                self._session_locks[session_key] = threading.Lock()
            return self._session_locks[session_key]

    def _process_inbound(self, event: InboundEvent) -> None:
        # 1) session_key 构建（全项目唯一公式）
        key = build_session_key(event.source, agent_id=self.agent_id)
        # 2) 同 key 加锁串行，保证同一会话不被并发打断
        with self._lock_for(key):
            try:
                reply = self._run_turn_locked(key, event)
            except Exception as e:
                logger.exception("处理入站事件失败 key=%s", key)
                reply = OutboundReply(
                    text="",
                    source=event.source,
                    reply_to=event.source.reply_to_message_id,
                    metadata={
                        "correlation_id": event.metadata.get("correlation_id"),
                        "error": str(e),
                    },
                )
        self.bus.publish_outbound(reply)

    def _run_turn_locked(self, session_key: str, event: InboundEvent) -> OutboundReply:
        corr = event.metadata.get("correlation_id")

        # ---- 斜杠命令：Gateway 短路，不进 Loop ----
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
                metadata={"correlation_id": corr},
            )
        if text == "/help":
            return OutboundReply(
                text="可用命令：/new 或 /reset 新开会话；/help 查看帮助。其他内容直接对话。",
                source=event.source,
                reply_to=event.source.reply_to_message_id,
                metadata={"correlation_id": corr},
            )

        # ---- 上下文寻找：session_key → Session（没有就新建）----
        session = self.store.get_or_create(
            session_key,
            channel=event.source.channel,
            author_id=event.source.user_id or "",
        )
        # ---- 消息构建 → 交给 Loop 跑一轮 ----
        user_msg = self.build_message(event)
        result = self.loop.run_turn(
            session,
            user_msg,
            ephemeral_prompt=event.channel_prompt,
            session_key=session_key,
        )
        self.store.save(session)

        text_out = result.final_text if result.completed else (result.error or "处理失败")
        return OutboundReply(
            text=text_out or "",
            source=event.source,
            reply_to=event.source.reply_to_message_id,
            metadata={"correlation_id": corr},
        )

    # ================= 内部：出站分发 =================
    def _outbound_dispatch_loop(self) -> None:
        """从 out_dto 取回复：先唤醒同步等待者，再按 source.channel 路由投递。"""
        while self._running:
            try:
                reply = self.bus.next_outbound(timeout=0.5)
            except queue.Empty:
                continue
            if reply is None:  # 结束信号
                break

            corr = (reply.metadata or {}).get("correlation_id")
            if corr:
                with self._waiter_lock:
                    waiters = self._reply_waiters.get(corr, [])
                for cb in waiters:
                    try:
                        cb(reply)
                    except Exception:
                        logger.exception("同步等待者回调失败")

            ch = self._channels.get(reply.source.channel)
            if ch is None:
                logger.warning("出站找不到 channel=%s", reply.source.channel)
                continue
            try:
                ch.deliver_to_OutboundEvent(reply)
                ch.status["outbound_event_count"] += 1
                self.events.publish(
                    LoopEvent(
                        type="outbound_reply",
                        session_id="",
                        data={
                            "channel": reply.source.channel,
                            "text_preview": reply.text[:300],
                            "error": (reply.metadata or {}).get("error"),
                        },
                    )
                )
            except Exception:
                logger.exception("出站投递失败 channel=%s", reply.source.channel)

    # ================= 观测 =================
    def status(self) -> dict:
        """gateway 运行状态快照（/api/status 用）。"""
        return {
            "agent_id": self.agent_id,
            "running": self._running,
            "channels": {
                name: ch.get_status() for name, ch in self._channels.items()
            },
            "bus": {"inbound": self.bus.inbound_size, "outbound": self.bus.outbound_size},
            "sessions": self.store.list_sessions(),
            "model": getattr(self.loop.model, "name", "unknown"),
            "tools": self.loop.tools.list_tools() if self.loop.tools else [],
            "skills": self.loop.skills.list_skills() if self.loop.skills else [],
        }
