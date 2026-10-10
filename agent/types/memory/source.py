"""Session 事实进入 Memory 审查流程时使用的不可变信封。"""
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, ConfigDict

from agent.types.memory.scope import MemoryScope


# 职责：固定一次轮次的来源 ID、状态、scope 与时间范围。
# 边界：不装载消息正文；Worker 根据这些 ID 到 SessionStore 读取真实原文。
# 关联：Gateway 生成 TurnRecord，Scheduler 据 completed/interrupted 决定是否排队。


class EvidenceRef(BaseModel):
    """用户卡片引用的原始消息、工具结果或历史手册版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_type: str
    source_id: str
    source_revision: str | None = None
    source_path: str | None = None
    source_hash: str | None = None
    excerpt: str | None = None
    observed_at: datetime | None = None


class TurnRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn_id: str
    session_id: str
    scope: MemoryScope
    user_message_id: str
    assistant_message_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    started_at: float
    finished_at: float
    completed: bool
    interrupted: bool
    error: str | None
    # 可选的 Precision Trace 持久化投影；使用普通映射，避免本信封依赖 Precision 实现。
    precision_trace: dict | None = None
