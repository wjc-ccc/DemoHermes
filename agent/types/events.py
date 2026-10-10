"""events 数据契约。"""
from __future__ import annotations
import time
import uuid
from typing import Literal
from pydantic import BaseModel as PydanticModel, Field


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
