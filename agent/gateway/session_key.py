"""session_key 确定性公式 — 全项目唯一实现点。"""
from __future__ import annotations

from agent.types.channel import SessionSource


def build_session_key(source: SessionSource, agent_id: str = "main") -> str:
    """
    DM:    agent:{id}:{channel}:dm:{chat_id}[:user:{uid}][:thread:{tid}]
    Group: agent:{id}:{channel}:group:{chat_id}[:user:{uid}][:thread:{tid}]

    DM 与 Group 都带 user：build_memory_scope 的 DM scope 按 user_id 归属，
    同一槽位只能对应一个 scope，否则 SessionStore 会拒绝这个槽位。
    """
    parts = [f"agent:{agent_id}", source.channel, source.chat_type, source.chat_id]
    if source.chat_type in ("dm", "group") and source.user_id:
        parts.append(f"user:{source.user_id}")
    if source.thread_id:
        parts.append(f"thread:{source.thread_id}")
    return ":".join(parts)
