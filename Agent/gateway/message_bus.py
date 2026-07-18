"""
MessageBus — 进程内消息总线（入站/出站双队列）

数据流向：
    Channel --publish_inbound--> [in_dto]  --Gateway worker 消费-->
    [out_dto] --publish_outbound--> Channel.deliver_to_OutboundEvent

这不是独立进程的 message bus，而是同进程 queue.Queue 的封装：
    - 各 Channel 只 put，不碰 Session/Loop
    - Gateway 是唯一消费者/生产者枢纽
    - 要分布式时把内部 Queue 换成 Redis List 即可，接口不变

None 是结束信号（sentinel）：close() 后消费者收到 None 退出。
"""
from __future__ import annotations

import queue
from typing import Generic, TypeVar

from ..core.types import InboundEvent, OutboundReply

T = TypeVar("T")


"""
本质两个queue，一个负责进入一个负责出，消费者和生产者只有gateway
提供的功能就是put、get、大小约束、结束信号、长度观测
"""


class _DtoQueue(Generic[T]):
    """单条 DTO 队列：put / get / 结束信号 / 长度观测。"""

    def __init__(self, name: str, maxsize: int = 0):
        self.name = name
        self._q: queue.Queue[T | None] = queue.Queue(maxsize=maxsize)

    def put(self, item: T) -> None:
        self._q.put(item)

    def get(self, timeout: float | None = None) -> T | None:
        """timeout 到期抛 queue.Empty；收到 None 表示总线已关闭。"""
        return self._q.get(block=True, timeout=timeout)

    def put_sentinel(self) -> None:
        """结束信号，worker 收到 None 后退出。"""
        self._q.put(None)

    def qsize(self) -> int:
        return self._q.qsize()


class MessageBus:
    """一对入站/出站 DTO 队列，Gateway 与 Channel 之间的唯一传送带。"""

    def __init__(self, maxsize: int = 0):
        self._inbound: _DtoQueue[InboundEvent] = _DtoQueue("in_dto", maxsize=maxsize)
        self._outbound: _DtoQueue[OutboundReply] = _DtoQueue("out_dto", maxsize=maxsize)

    # ---- 输入侧：Channel → Gateway ----
    def publish_inbound(self, event: InboundEvent) -> None:
        self._inbound.put(event)

    def next_inbound(self, timeout: float | None = None) -> InboundEvent | None:
        return self._inbound.get(timeout=timeout)

    # ---- 输出侧：Gateway → Channel ----
    def publish_outbound(self, reply: OutboundReply) -> None:
        self._outbound.put(reply)

    def next_outbound(self, timeout: float | None = None) -> OutboundReply | None:
        return self._outbound.get(timeout=timeout)

    # ---- 观测与生命周期 ----
    @property
    def inbound_size(self) -> int:
        return self._inbound.qsize()

    @property
    def outbound_size(self) -> int:
        return self._outbound.qsize()

    def close(self) -> None:
        """双侧发结束信号，消费线程收到 None 后自行退出。"""
        self._inbound.put_sentinel()
        self._outbound.put_sentinel()
