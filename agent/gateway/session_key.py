"""session_key 确定性公式 — 全项目唯一实现点。"""
from __future__ import annotations

from ..core.types import SessionSource


def build_session_key(source: SessionSource, agent_id: str = "main") -> str:
    """
    DM:    agent:{id}:{channel}:dm:{chat_id}[:thread:{tid}]
    Group: agent:{id}:{channel}:group:{chat_id}:user:{uid}[:thread:{tid}]
    """
    parts = [f"agent:{agent_id}", source.channel, source.chat_type, source.chat_id]
    if source.chat_type == "group" and source.user_id:
        parts.append(f"user:{source.user_id}")
    if source.thread_id:
        parts.append(f"thread:{source.thread_id}")
    return ":".join(parts)
