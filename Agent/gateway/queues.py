"""
Gateway 进程内双队列：InDTO / OutDTO

这不是独立进程的 message bus，而是同进程 queue.Queue：
    Channel --put--> in_queue  --worker--> out_queue --get--> Channel.deliver
"""
from __future__ import annotations

import queue
from typing import Generic, TypeVar

from ..core.types import InboundEvent, OutboundReply

T = TypeVar("T")


class DtoQueue(Generic[T]):
    def __init__(self, name: str, maxsize: int = 0):
        self.name = name
        self._q: queue.Queue[T | None] = queue.Queue(maxsize=maxsize)

    def put(self, item: T, *, block: bool = True, timeout: float | None = None) -> None:
        self._q.put(item, block=block, timeout=timeout)

    def get(self, *, block: bool = True, timeout: float | None = None) -> T | None:
        """timeout 到期抛 queue.Empty。"""
        return self._q.get(block=block, timeout=timeout)

    def put_sentinel(self) -> None:
        """结束信号，worker 收到 None 后退出。"""
        self._q.put(None)

    def qsize(self) -> int:
        return self._q.qsize()

    def task_done(self) -> None:
        self._q.task_done()


class GatewayQueues:
    """一对入站/出站 DTO 队列。"""

    def __init__(self, maxsize: int = 0):
        self.inbound: DtoQueue[InboundEvent] = DtoQueue("in_dto", maxsize=maxsize)
        self.outbound: DtoQueue[OutboundReply] = DtoQueue("out_dto", maxsize=maxsize)

    def close(self) -> None:
        self.inbound.put_sentinel()
        self.outbound.put_sentinel()
