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
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..channel.baseChannel import BaseChannel
from ..core.event_bus import EventBus, LoopEvent, default_event_bus
from ..core.agent_loop import AgentLoop
from ..core.building_messages import build_assistant_message, build_user_message
from ..core.types import InboundEvent, Message, OutboundReply
from ..core.memory import MemoryContextLoader, build_memory_scope
from ..core.memory.types import TurnRecord
from .message_bus import MessageBus
from .session_key import build_session_key
from .session_store import DictSessionStore

logger = logging.getLogger(__name__)

# 同步 ask 的默认等待上限（秒）
ASK_TIMEOUT = 120.0

# messagebus + agentloop + channels + locks + threadpool + sessiondict + runningstate
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
        memory_context: MemoryContextLoader | None = None,
        on_session_end=None,
        on_turn_committed=None,
        reviewer_worker=None,
        context_budget=None,
        token_estimator=None,
        signal_collector=None,
        assembly_policy=None,
        assembly_recorder=None,
        skill_selector=None,
        observe_databases: dict[str, Path] | None = None,
        action_runtime=None,
    ):
        self.agent_id = agent_id
        self.loop = loop or AgentLoop()          # 对话核心
        self.store = store or DictSessionStore()  # session_key → Session（dict）
        self.bus = bus or MessageBus()            # 入站/出站双队列
        self.events = event_bus or default_event_bus 
        self._memory_context = memory_context  # 可选；每 turn 只冻结一次 MemorySnapshot
        self._on_session_end = on_session_end  # 可选；reset 事务 commit 后才对 closed_session 触发（§8.3）
        self._on_turn_committed = on_turn_committed  # 可选；commit_turn 成功后唤醒 reviewer
        self._reviewer_worker = reviewer_worker  # 可选；随 Gateway start/stop 启停
        self._context_budget = context_budget  # 可选；整轮 prompt token 预算（8.04 Phase 0.1）
        self._token_estimator = token_estimator  # 可选；与 context_budget 配套的 TokenEstimator
        self._signal_collector = signal_collector  # 可选；EventBus → evolution signals（8.04 Phase 0.4）
        self._assembly_policy = assembly_policy  # 可选；8.04 Phase 4 装配策略
        self._assembly_recorder = assembly_recorder  # 可选；记录每 turn 装配选择
        self._skill_selector = skill_selector  # 可选；Hermes 式按 query 选 L1 Skill
        self.observe_databases = observe_databases or {}
        self._action = action_runtime
        self._channels: dict[str, BaseChannel] = {} # channel 注册与管理
        # 同 session_key 串行：每 key 一把锁
        self._session_locks: dict[str, threading.Lock] = {} # 同 session_key 串行：每 key 一把锁
        self._locks_guard = threading.Lock() # 同 session_key 串行：每 key 一把锁
        self._executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="gw-in") # 线程池
        self._threads: list[threading.Thread] = [] # 线程池
        self._running = False # 运行状态
        # 同步 ask 的等待者：correlation_id → 回调
        self._reply_waiters: dict[str, list] = {} # 同步 ask 的等待者：correlation_id → 回调
        self._waiter_lock = threading.Lock() # 同步 ask 的等待者：correlation_id → 回调

    # ================= Channel 注册 ================= ✅️
    def register_channel(self, channel: BaseChannel) -> None:
        """注册 Channel 并回绑 Gateway（Channel.send_gateway 才有去处）。"""
        self._channels[channel.name] = channel
        channel.bind_gateway(self)
        logger.info("注册 channel=%s", channel.name)

    def unregister_channel(self, name: str) -> None:
        channel = self._channels.pop(name, None)
        if channel is not None:
            channel.unbind_gateway()

    def get_channel(self, name: str) -> BaseChannel | None:
        return self._channels.get(name)

    # ================= 生命周期 =================  ✅️
    # threading.Thread(target=xx)创建, t.start()启动gateway的两个线程  
    # 分别启动监听messagebus的inbound和outbound
    def start(self) -> None:
        if self._running:
            return
        # ACTION-HOOK-09：先查证遗留动作再接收请求；关闭模块时无此生命周期。
        if self._action is not None:
            self._action.recover()
        self._running = True
        self._threads = [
            threading.Thread(target=self._inbound_dispatch_loop, name="gw-in-dispatch", daemon=True),
            threading.Thread(target=self._outbound_dispatch_loop, name="gw-out-dispatch", daemon=True),
        ]
        for t in self._threads:
            t.start()
        if self._reviewer_worker is not None:
            self._reviewer_worker.start()
        if self._signal_collector is not None:
            self._signal_collector.start()
        logger.info(
            "gateway 已启动 workers=%s channels=%s",
            self._executor._max_workers, list(self._channels),
        )

    # ================= 生命周期结束 =================  ✅️
    # 首先设置监督flag _running为false 然后关闭message bus 最后通过shutdown函数关闭线程池
    def stop(self) -> None:
        self._running = False
        for name, channel in list(self._channels.items()):
            try:
                channel.stop()
            except Exception:
                logger.exception("停止 channel=%s 失败", name)
        self.bus.close()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._reviewer_worker is not None:
            self._reviewer_worker.stop()
        if self._signal_collector is not None:
            self._signal_collector.stop()
        if self._action is not None:
            self._action.stop()
        logger.info("gateway 已停止")

    # ================= 消息构建 =================  ✅️
    @staticmethod
    def build_message(event: InboundEvent) -> Message:
        """InboundEvent → role=user 的 Message（统一走 core/messages 工厂）。"""
        return build_user_message(event)

    # ================= 入站入口 =================    ✅️
    def submit(self, event: InboundEvent) -> None:
        """异步：解析好的 InboundEvent 入队，不等待回复。"""
        self.bus.publish_inbound(event)

    def submit_raw(self, channel_name: str, raw) -> None:
        """原始平台载荷：先走 Channel.parse_to_InboundEvent，再入队。"""
        ch = self._channels.get(channel_name)
        if ch is None:
            raise KeyError(f"channel 未注册: {channel_name}")
        self.submit(ch.parse_to_InboundEvent(raw))

    # TODO 等待理解ask函数的实现
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

    # ================= 线程task1：入站调度 ================= ✅️
    # 通过submit函数将事件提交到线程池中 去调用process_inbound函数  每timeout检查一次inbound queue
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

    # ================= 核心：获取锁 ================= ✅️
    def _lock_for(self, session_key: str) -> threading.Lock:
        with self._locks_guard:
            if session_key not in self._session_locks:
                self._session_locks[session_key] = threading.Lock()
            return self._session_locks[session_key]

    # ================= 核心：处理入站事件 ================= ✅️
    # 本质首先获取 key 然后lock主 然后根据key去run_turn_locked函数去处理事件
    def _process_inbound(self, event: InboundEvent) -> None:
        # 1) session_key 构建
        key = build_session_key(event.source, agent_id=self.agent_id)
        # 2) 同 key 加锁串行，保证同一会话不被并发打断
        with self._lock_for(key):
            started = time.time()
            user_msg = None
            failure_state = {}
            try:
                user_msg = self.build_message(event)
                reply = self._run_turn_locked(key, event, user_msg, failure_state)
            except Exception as e:
                logger.exception("处理入站事件失败 key=%s", key)
                reply = self._record_unhandled_failure(
                    key, event, user_msg, e, started, failure_state,
                )
        self.bus.publish_outbound(reply)

    @staticmethod
    def _append_gateway_error(session, text: str) -> Message:
        message = build_assistant_message(text)
        message.status = "failed"
        message.metadata["origin"] = "gateway_error"
        return session.add_message(message)

    @staticmethod
    def _mark_failed_turn_messages(session, user_msg: Message) -> None:
        for message in session.messages:
            if message.seq_id > user_msg.seq_id:
                message.status = "failed"
                message.metadata["origin"] = "failed_turn"

    def _record_unhandled_failure(
        self, session_key: str, event: InboundEvent, user_msg: Message | None,
        error: Exception, started: float, failure_state: dict,
    ) -> OutboundReply:
        text_out = "处理失败，请稍后重试。"
        metadata = {
            "correlation_id": event.metadata.get("correlation_id"),
            "session_key": session_key,
            "error": str(error),
        }
        try:
            channel = event.source.channel
            author_id = event.source.user_id or ""
            session = failure_state.get("session")
            scope = failure_state.get("scope")
            if self._memory_context is not None:
                if session is None or scope is None:
                    base_scope = build_memory_scope(event.source)
                    if session is None:
                        session = self.store.get_or_create(
                            session_key, scope=base_scope, channel=channel, author_id=author_id,
                        )
                    scope = base_scope.model_copy(
                        update={"session_id": session.session_id, "task_id": session.session_id}
                    )
            elif session is None:
                session = self.store.get_or_create(
                    session_key, channel=channel, author_id=author_id,
                )
            if user_msg is None:
                user_msg = Message(role="user", content_text=event.text or "", author_id=author_id)
            if not any(message.message_id == user_msg.message_id for message in session.messages):
                session.add_message(user_msg)
            self._mark_failed_turn_messages(session, user_msg)
            failure_message = self._append_gateway_error(session, text_out)
            turn_id = user_msg.trajectory_id or uuid.uuid4().hex[:12]
            metadata.update({"turn_id": turn_id, "session_id": session.session_id})
            if scope is not None:
                turn = TurnRecord(
                    turn_id=turn_id,
                    session_id=session.session_id,
                    scope=scope.model_copy(update={"turn_id": turn_id}),
                    user_message_id=user_msg.message_id,
                    assistant_message_ids=tuple(
                        message.message_id for message in session.messages
                        if message.role == "assistant" and message.seq_id > user_msg.seq_id
                    ),
                    tool_call_ids=tuple(
                        call.tool_call_id for call in session.tool_calls
                        if call.trajectory_id == turn_id
                    ),
                    started_at=started,
                    finished_at=time.time(),
                    completed=False,
                    interrupted=False,
                    error=str(error),
                )
                self.store.commit_turn(session, turn, scope=scope)
            else:
                self.store.save(session)
        except Exception as persistence_error:
            logger.exception("异常轮次落库失败 key=%s", session_key)
            metadata["persistence_error"] = str(persistence_error)
        return OutboundReply(
            text=text_out,
            source=event.source,
            reply_to=event.source.reply_to_message_id,
            metadata=metadata,
        )

    # ================= 核心：处理入站事件 =================  ✅️
    # 本质首先解析text 然后判断是否为斜杠命令 不是的话根据key获取session上下文 然后把session 和message加入进去 然后交给loop去跑一轮
    def _run_turn_locked(
        self, session_key: str, event: InboundEvent, user_msg: Message, failure_state: dict,
        *, assembly_policy=None,
    ) -> OutboundReply:
        corr = event.metadata.get("correlation_id")
        if self._memory_context is not None:
            return self._run_turn_persistent(session_key, event, user_msg, corr, failure_state,
                                             assembly_policy=assembly_policy)

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
        failure_state["session"] = session
        # ---- 消息构建 → 交给 Loop 跑一轮 ----
        result = self.loop.run_turn(
            session,
            user_msg,
            ephemeral_prompt=event.channel_prompt,
            session_key=session_key,
            memory_scope=build_memory_scope(event.source) if self._action is not None else None,
            assembly_policy=assembly_policy or self._assembly_policy,
            agent_id=self.agent_id,
        )
        text_out = result.final_text if result.completed else (result.error or "处理失败")
        if not result.completed:
            self._mark_failed_turn_messages(session, user_msg)
            self._append_gateway_error(session, text_out)
        self.store.save(session)
        return OutboundReply(
            text=text_out or "",
            source=event.source,
            reply_to=event.source.reply_to_message_id,
            metadata={
                "correlation_id": corr,
                "turn_id": result.turn_id,
                "session_id": session.session_id,
                "session_key": session_key,
            },
        )

    def _run_turn_persistent(
        self, session_key: str, event: InboundEvent, user_msg: Message, corr,
        failure_state: dict,
        *, assembly_policy=None,
    ) -> OutboundReply:
        """持久化路径：解析 scope、冻结 Memory、执行 Turn、原子提交事实。"""
        base_scope = build_memory_scope(event.source)
        text = (event.text or "").strip()
        channel = event.source.channel
        author_id = event.source.user_id or ""

        if text in {"/new", "/reset"}:
            session = self.store.get_or_create(
                session_key, scope=base_scope, channel=channel, author_id=author_id,
            )
            scope = base_scope.model_copy(
                update={"session_id": session.session_id, "task_id": session.session_id}
            )
            reset = self.store.close_and_reset(
                session_key, session, scope=scope, channel=channel, author_id=author_id,
            )
            # 事务 commit 成功后才对 closed_session 触发 session_end hook
            if self._on_session_end is not None:
                self._on_session_end(reset.closed_session)
            return OutboundReply(
                text=f"已新开会话 session_id={reset.new_session.session_id}",
                source=event.source, reply_to=event.source.reply_to_message_id,
                metadata={"correlation_id": corr},
            )
        if text == "/help":
            return OutboundReply(
                text="可用命令：/new 或 /reset 新开会话；/help 查看帮助。其他内容直接对话。",
                source=event.source, reply_to=event.source.reply_to_message_id,
                metadata={"correlation_id": corr},
            )

        session = self.store.get_or_create(
            session_key, scope=base_scope, channel=channel, author_id=author_id,
        )
        failure_state["session"] = session
        scope = base_scope.model_copy(
            update={"session_id": session.session_id, "task_id": session.session_id}
        )
        failure_state["scope"] = scope
        snapshot = self._memory_context.freeze(
            scope, session_id=session.session_id, query=event.text or ""
        )
        started = time.time()
        result = self.loop.run_turn(
            session, user_msg,
            memory_snapshot=snapshot,
            memory_scope=scope,
            ephemeral_prompt=event.channel_prompt,
            session_key=session_key,
            context_budget=self._context_budget,
            token_estimator=self._token_estimator,
            skill_query=event.text or "",
            skill_selector=self._skill_selector,
            assembly_policy=assembly_policy or self._assembly_policy,
            agent_id=self.agent_id,
        )
        if self._assembly_recorder is not None:
            from ..evolution.assembly import AssemblyChoice

            policy = self._assembly_policy
            skill_chars = 0
            if self.loop.skills is not None:
                skill_chars = len(
                    self.loop.skills.summaries_prompt(
                        query=event.text or "",
                        selector=self._skill_selector,
                        scope_key=scope.storage_key,
                    )
                    or ""
                )
            self._assembly_recorder.record(
                AssemblyChoice(
                    turn_id=result.turn_id,
                    skill_summary_chars=skill_chars,
                    skill_summary_min_chars=(
                        policy.skill_summary_min_chars if policy is not None else None
                    ),
                )
            )
        precision_trace = getattr(self.loop, "precision_trace", None)
        precision_turn = (
            precision_trace.get(result.turn_id)
            if precision_trace is not None
            else None
        )
        text_out = result.final_text if result.completed else (result.error or "处理失败")
        if not result.completed:
            self._mark_failed_turn_messages(session, user_msg)
            self._append_gateway_error(session, text_out)
        turn = TurnRecord(
            turn_id=result.turn_id,
            session_id=session.session_id,
            scope=scope.model_copy(update={"turn_id": result.turn_id}),
            user_message_id=user_msg.message_id,
            assistant_message_ids=tuple(
                m.message_id
                for m in session.messages
                if m.role == "assistant" and m.seq_id > user_msg.seq_id
            ),
            tool_call_ids=tuple(
                tc.tool_call_id
                for tc in session.tool_calls
                if tc.trajectory_id == result.turn_id
            ),
            started_at=started,
            finished_at=time.time(),
            completed=result.completed,
            interrupted=result.interrupted,
            error=result.error,
            precision_trace=(
                precision_turn.model_dump(mode="json") if precision_turn is not None else None
            ),
        )
        # SQLite 提交失败不丢模型回复--返回 reply 但 metadata 标 persistence_error
        metadata = {
            "correlation_id": corr,
            "turn_id": result.turn_id,
            "session_id": session.session_id,
            "session_key": session_key,
        }
        if result.interrupted:
            metadata["interrupted"] = True
        if not result.completed and result.error:
            metadata["error"] = result.error
        try:
            self.store.commit_turn(session, turn, scope=scope)
        except Exception as exc:
            logger.exception("commit_turn 失败 turn_id=%s", result.turn_id)
            metadata["persistence_error"] = str(exc)
            if any(
                call.trajectory_id == result.turn_id
                and call.tool_name == "memory_edit"
                and isinstance(call.tool_call_result, dict)
                and call.tool_call_result.get("memory_mutation") is not None
                for call in session.tool_calls
            ):
                metadata["error"] = "memory_write_failed"
                text_out = "记忆写入失败，本轮修改未保存。请重试。"
        else:
            # 事实落库成功后才唤醒 reviewer（§8.1：提交失败则不启后台提炼）
            if self._on_turn_committed is not None:
                try:
                    self._on_turn_committed(turn)
                except Exception:
                    logger.exception("on_turn_committed hook 失败 turn_id=%s", result.turn_id)
        return OutboundReply(
            text=text_out or "", source=event.source,
            reply_to=event.source.reply_to_message_id,
            metadata=metadata,
        )

    # ================= 线程task2：出站分发 ================= ✅️
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
                meta = reply.metadata or {}
                self.events.publish(
                    LoopEvent(
                        type="outbound_reply",
                        turn_id=str(meta.get("turn_id") or ""),
                        session_id=str(meta.get("session_id") or ""),
                        session_key=str(meta.get("session_key") or ""),
                        data={
                            "channel": reply.source.channel,
                            "text_preview": reply.text[:300],
                            "error": meta.get("error"),
                        },
                    )
                )
            except Exception:
                ch.status["error_count"] += 1
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
            "memory_enabled": self._memory_context is not None,
            "model": getattr(self.loop.model, "name", "unknown"),
            "tools": self.loop.tools.list_tools() if self.loop.tools else [],
            "skills": self.loop.skills.list_skills() if self.loop.skills else [],
        }
