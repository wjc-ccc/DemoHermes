"""
Gateway — 进程内消息枢纽（MessageBus 双队列 + 按 session_key 排队）

核心能力：
    1. Channel 注册与管理（register_channel / get_channel）
    2. MessageBus 输入输出（submit/ask 入站，_outbound_dispatch_loop 出站分发）
    3. session_key 构建与上下文寻找（build_session_key + SessionStore）
    4. 同 session_key 按到达顺序串行（每 key 一个待处理队列），不同 key 并行（线程池）

数据流：
    Channel.parse_to_InboundEvent(raw)
        → submit/ask → [inbound]
        → dispatch：build_session_key → 进该 key 的待处理队列
        → worker：store.get_or_create → AgentLoop.run_turn → store.commit_turn
        → [outbound] → 按 source.channel 路由 → Channel.deliver_to_OutboundEvent

Gateway 由装配入口注入 SQLite 会话存储，所有请求走同一提交路径；
memory_context 为空时只是不冻结 MemorySnapshot。

斜杠命令（/new /reset /help）在 Gateway 短路处理，不进 Loop。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import partial

from ..channel.baseChannel import BaseChannel
from ..agent_loop import AgentLoop
from ..building_messages import build_assistant_message, build_user_message
from agent.core.event_bus import EventBus
from agent.types.events import LoopEvent
from agent.core.memory.memory_context_loader import MemoryContextLoader
from agent.types.memory.scope import MemoryScope
from agent.core.memory.memory_scope import build_memory_scope
from agent.types.memory.source import TurnRecord
from agent.types.channel import InboundEvent, OutboundReply
from agent.types.conversation import Message, Session, TurnResult
from agent.types.runtime import RuntimeDescription
from .message_bus import MessageBus
from .session_key import build_session_key

logger = logging.getLogger(__name__)

# 同步 ask 的默认等待上限（秒）
ASK_TIMEOUT = 120.0

_HELP_TEXT = "可用命令：/new 或 /reset 新开会话；/help 查看帮助。其他内容直接对话。"
_FAILURE_TEXT = "处理失败，请稍后重试。"
# 回复正文恰好等于它时只落库、唤醒等待者，不投递到 channel（后台唤醒判断无需打扰用户时使用）
SILENT_REPLY = "[NO_NOTIFY]"


class Gateway:
    def __init__(
        self,
        *,
        loop: AgentLoop,
        store,
        event_bus: EventBus | None = None,
        workers: int = 2,
        memory_context: MemoryContextLoader | None = None,
        on_session_end=None,
        on_turn_committed=None,
        action_runtime=None,
        perception_trace=None,
        describe_agent=None,
    ):
        self.loop = loop  # 对话核心；每轮不变的装配配置都在 Loop 构造时注入
        self.agent_id = getattr(loop, "agent_id", "main")
        self.store = store
        self.bus = MessageBus()                   # 入站/出站双队列
        self.events = event_bus if event_bus is not None else EventBus()
        self._memory_context = memory_context  # 可选；每 turn 只冻结一次 MemorySnapshot
        self.on_session_end = on_session_end  # 可选；reset 事务 commit 后才对 closed_session 触发（§8.3）
        self.on_turn_committed = on_turn_committed  # 可选；commit_turn 成功后唤醒 reviewer
        self._action = action_runtime
        self.perception_trace = perception_trace
        self.describe_agent = describe_agent
        self._channels: dict[str, BaseChannel] = {}
        self._workers = max(1, workers)
        self._executor = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="gw-in")
        # key 在表中 = 该会话有任务正在执行；deque 里是按到达顺序排在它后面的任务
        self._pending: dict[str, deque[tuple]] = {}
        self._pending_guard = threading.Lock()
        self._closed = False
        self._threads: list[threading.Thread] = []
        self._running = False
        self._reply_waiters: dict[str, Future] = {}
        self._waiter_lock = threading.Lock()

    # ================= Channel 注册 =================
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
        logger.info("gateway 已启动 workers=%s channels=%s", self._workers, list(self._channels))

    def stop(self) -> None:
        self._running = False
        self._closed = True
        for name, channel in list(self._channels.items()):
            try:
                channel.stop()
            except Exception:
                logger.exception("停止 channel=%s 失败", name)
        self.bus.close()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        self._executor.shutdown(wait=False)
        logger.info("gateway 已停止")

    # ================= 入站入口 =================
    def submit(self, event: InboundEvent) -> None:
        """异步：解析好的 InboundEvent 入队，不等待回复。"""
        self.bus.publish_inbound(event)

    def ask(self, event: InboundEvent, *, timeout: float = ASK_TIMEOUT) -> OutboundReply:
        """
        同步问一句：仍走完整 MessageBus 链路，
        用 correlation_id 在出站侧唤醒等待者（HTTP / CLI 同步场景用）。
        """
        corr = uuid.uuid4().hex
        event = event.model_copy(update={"metadata": {**event.metadata, "correlation_id": corr}})
        waiter: Future = Future()
        with self._waiter_lock:
            self._reply_waiters[corr] = waiter
        self.submit(event)
        try:
            return waiter.result(timeout=timeout)
        except FutureTimeoutError:
            return OutboundReply(
                text="", source=event.source, metadata={"error": "等待 Agent 回复超时"},
            )
        finally:
            with self._waiter_lock:
                self._reply_waiters.pop(corr, None)

    # ================= 线程task1：入站调度 =================
    def _inbound_dispatch_loop(self) -> None:
        """持续从 inbound 取事件，按 session_key 进各自的待处理队列。"""
        while self._running:
            try:
                event = self.bus.next_inbound(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:  # 结束信号
                break
            key = build_session_key(event.source, agent_id=self.agent_id)
            self._schedule(key, partial(self._process_inbound, key, event))

    # ================= 核心：按 session_key 排队 =================
    def _schedule(self, session_key: str, job) -> Future:
        """同 key 按到达顺序串行；排队的任务不占 worker，空闲 key 才提交线程池。"""
        future: Future = Future()
        with self._pending_guard:
            waiting = self._pending.get(session_key)
            if waiting is not None:
                waiting.append((job, future))
                return future
            self._pending[session_key] = deque()
        self._submit(session_key, job, future)
        return future

    def _submit(self, session_key: str, job, future: Future) -> None:
        try:
            self._executor.submit(self._run_job, session_key, job, future)
        except RuntimeError as exc:  # 线程池已 shutdown
            with self._pending_guard:
                dropped = [future, *(f for _, f in self._pending.pop(session_key, ()))]
            for item in dropped:
                item.set_exception(exc)

    def _run_job(self, session_key: str, job, future: Future) -> None:
        try:
            if self._closed:
                raise RuntimeError("gateway 已停止")
            future.set_result(job())
        except Exception as exc:
            future.set_exception(exc)
        finally:
            with self._pending_guard:
                waiting = self._pending[session_key]
                following = waiting.popleft() if waiting else None
                if following is None:
                    del self._pending[session_key]
            if following is not None:
                self._submit(session_key, *following)

    # ACTION-HOOK-10：显式计划入口在 Loop 外调度；每段复用正常持久化 turn。
    # 关闭 action 时拒绝计划任务，不隐式改变普通对话模式。
    def _require_plan_runtime(self):
        if self._action is None:
            raise ValueError("计划执行需要 action")

    def create_plan(self, plan, source):
        self._require_plan_runtime()
        scope = build_memory_scope(source)
        return self._action.create_plan(plan, scope_key=scope.storage_key,
                                        actor_id=scope.user_id or "", agent_id=self.agent_id)

    def run_plan(self, plan_id: str, source):
        self._require_plan_runtime()
        key = build_session_key(source, agent_id=self.agent_id)
        scope = build_memory_scope(source)

        def run_step(step, segment, prompt, policy):
            event = InboundEvent(text=prompt, source=source)
            job = partial(
                self._run_turn, key, event, assembly_policy=policy,
                message_metadata={"source_type": "system_injected", "origin": "action_plan"},
            )
            return self._schedule(key, job).result()

        return self._action.run_plan(plan_id, run_step, scope_key=scope.storage_key,
                                     actor_id=scope.user_id or "", agent_id=self.agent_id)

    # ================= 核心：处理入站事件 =================
    def _process_inbound(self, session_key: str, event: InboundEvent) -> None:
        try:
            reply = self._handle(session_key, event)
        except Exception as exc:
            logger.exception("处理入站事件失败 key=%s", session_key)
            reply = self._reply(event, _FAILURE_TEXT, session_key=session_key, error=str(exc))
        self.bus.publish_outbound(reply)

    def _handle(self, session_key: str, event: InboundEvent) -> OutboundReply:
        """斜杠命令在此短路，其余内容交给 Loop 跑一轮。"""
        text = (event.text or "").strip()
        if text == "/help":
            return self._reply(event, _HELP_TEXT)
        if text in {"/new", "/reset"}:
            session, scope = self._session_for(session_key, event)
            reset = self.store.close_and_reset(
                session_key, session, scope=scope,
                channel=event.source.channel, author_id=event.source.user_id or "",
            )
            # 事务 commit 成功后才对 closed_session 触发 session_end hook
            if self.on_session_end is not None:
                self.on_session_end(reset.closed_session)
            return self._reply(event, f"已新开会话 session_id={reset.new_session.session_id}")
        return self._run_turn(session_key, event)

    def _session_for(self, session_key: str, event: InboundEvent) -> tuple[Session, MemoryScope]:
        scope = build_memory_scope(event.source)
        session = self.store.get_or_create(
            session_key, scope=scope,
            channel=event.source.channel, author_id=event.source.user_id or "",
        )
        return session, scope

    def _run_turn(
        self, session_key: str, event: InboundEvent, *,
        assembly_policy=None, message_metadata: dict | None = None,
    ) -> OutboundReply:
        """冻结 Memory、执行 Turn、原子提交事实；Turn 内异常也按失败轮次落库。"""
        session, scope = self._session_for(session_key, event)
        user_msg = build_user_message(event)
        if message_metadata:
            user_msg.metadata.update(message_metadata)
        started = time.time()
        try:
            snapshot = (
                self._memory_context.freeze(scope, session_id=session.session_id, query=event.text or "")
                if self._memory_context is not None else None
            )
            result = self.loop.run_turn(
                session, user_msg,
                memory_snapshot=snapshot,
                memory_scope=scope,
                ephemeral_prompt=event.channel_prompt,
                assembly_policy=assembly_policy,
            )
            text_out = result.final_text if result.completed else (result.error or _FAILURE_TEXT)
        except Exception as exc:
            logger.exception("turn 执行失败 key=%s", session_key)
            if all(message.message_id != user_msg.message_id for message in session.messages):
                session.add_message(user_msg)
            result = TurnResult(
                session_id=session.session_id,
                turn_id=user_msg.trajectory_id or uuid.uuid4().hex[:12],
                completed=False,
                error=str(exc),
            )
            text_out = _FAILURE_TEXT
        if not result.completed:
            self._mark_failed_turn_messages(session, user_msg)
            self._append_gateway_error(session, text_out)

        perception_trace = self.perception_trace
        perception_turn = perception_trace.get(result.turn_id) if perception_trace is not None else None
        turn = TurnRecord(
            turn_id=result.turn_id,
            session_id=session.session_id,
            scope=scope,
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
            perception_trace=(
                perception_turn.model_dump(mode="json") if perception_turn is not None else None
            ),
            input_token_count=perception_turn.input_token_count if perception_turn is not None else None,
            output_token_count=perception_turn.output_token_count if perception_turn is not None else None,
        )
        metadata = {
            "turn_id": result.turn_id,
            "session_id": session.session_id,
            "session_key": session_key,
        }
        if not result.completed and result.error:
            metadata["error"] = result.error
        # 提交失败不丢模型回复--返回 reply 但 metadata 标 persistence_error
        try:
            self.store.commit_turn(session, turn, scope=scope)
        except Exception as exc:
            logger.exception("commit_turn 失败 turn_id=%s", result.turn_id)
            metadata["persistence_error"] = str(exc)
            if any(
                call.trajectory_id == result.turn_id
                and call.tool_name == "memory_edit"
                and call.pending_mutation is not None
                for call in session.tool_calls
            ):
                metadata["error"] = "memory_write_failed"
                text_out = "记忆写入失败，本轮修改未保存。请重试。"
        else:
            if perception_trace is not None:
                perception_trace.finish(result.turn_id)
            # 事实落库成功后才唤醒 reviewer（§8.1：提交失败则不启后台提炼）
            if self.on_turn_committed is not None:
                try:
                    self.on_turn_committed(turn)
                except Exception:
                    logger.exception("on_turn_committed hook 失败 turn_id=%s", result.turn_id)
        return self._reply(event, text_out or "", **metadata)

    @staticmethod
    def _reply(event: InboundEvent, text: str, **metadata) -> OutboundReply:
        return OutboundReply(
            text=text,
            source=event.source,
            metadata={"correlation_id": event.metadata.get("correlation_id"), **metadata},
        )

    @staticmethod
    def _append_gateway_error(session: Session, text: str) -> Message:
        message = build_assistant_message(text)
        message.status = "failed"
        message.metadata["origin"] = "gateway_error"
        return session.add_message(message)

    @staticmethod
    def _mark_failed_turn_messages(session: Session, user_msg: Message) -> None:
        for message in session.messages:
            if message.seq_id > user_msg.seq_id:
                message.status = "failed"
                message.metadata["origin"] = "failed_turn"

    # ================= 线程task2：出站分发 =================
    def _outbound_dispatch_loop(self) -> None:
        """从 outbound 取回复：先唤醒同步等待者，再按 source.channel 路由投递。"""
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
                    waiter = self._reply_waiters.pop(corr, None)
                if waiter is not None:
                    waiter.set_result(reply)

            if reply.text.strip() == SILENT_REPLY:
                continue
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
        description = self.describe_agent() if self.describe_agent is not None else RuntimeDescription(
            model=getattr(getattr(self.loop, "model", None), "name", "unknown"),
        )
        with self._pending_guard:
            queued = sum(len(waiting) for waiting in self._pending.values())
            busy = len(self._pending)
        return {
            "agent_id": self.agent_id,
            "running": self._running,
            "channels": {
                name: ch.status for name, ch in self._channels.items()
            },
            "bus": {"inbound": self.bus.inbound_size, "outbound": self.bus.outbound_size},
            "turns": {"busy_sessions": busy, "queued": queued},
            "sessions": self.store.list_sessions(),
            "memory_enabled": self._memory_context is not None,
            "model": description.model,
            "tools": list(description.tools),
            "skills": list(description.skills),
        }
