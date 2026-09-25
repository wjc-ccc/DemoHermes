"""
Events — 统一事件模型 + 进程内事件总线

Loop 每走一步就 publish 一个 LoopEvent，供两个用途：
    1. 可视化前端实时展示（frontier 前端经 SSE 订阅）
    2. 对话回放与复现（按事件序列重建完整上下文）
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import Literal

from pydantic import BaseModel as PydanticModel
from pydantic import Field

### 参考事件类型
EventType = Literal[
    "turn_start",
    "llm_request",
    "llm_response",
    "tool_call_start",
    "tool_call_result",
    "turn_end",
    "turn_error",
    "outbound_reply",
]

## 全局唯一事件类型 方便前端展示和回放
class LoopEvent(PydanticModel):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    type: str = Field(default_factory=str, description="事件类型，见模块 docstring")
    turn_id: str = Field(default_factory=str, description="一轮对话的标识，前端按它分组展示")
    session_key: str = Field(default_factory=str)
    session_id: str = Field(default_factory=str)
    data: dict = Field(default_factory=dict, description="各事件类型自定义载荷")
    created_at: float = Field(default_factory=time.time)


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


default_event_bus = EventBus()
