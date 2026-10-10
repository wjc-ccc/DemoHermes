"""
Events — 统一事件模型 + 进程内事件总线

Loop 每走一步就 publish 一个 LoopEvent，供两个用途：
    1. 可视化前端实时展示（frontier 前端经 SSE 订阅）
    2. 对话回放与复现（按事件序列重建完整上下文）
"""
from __future__ import annotations

import queue
import threading


from agent.types.events import LoopEvent

### 简单的管道 然后设定一个queue list 订阅的时候就加入一个queue 发布的时候然后通过lock锁住对应的queue list 然后通过put_nowait 放入对应的queue
class EventBus:
    """进程内发布/订阅总线。订阅满时丢事件（观测允许丢，主链路不允许堵）。"""

    def __init__(self) -> None:
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    ## 订阅event
    def subscribe(self, maxsize: int = 500) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    ## 取消订阅event
    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: LoopEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
